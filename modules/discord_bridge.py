import io
import json
import logging
import tempfile
import time
import typing
from pathlib import Path

import PIL.Image
import nio
import pydantic
import niobot
import websockets.client
import httpx
import aiosqlite
from util import config
from typing import Optional


class BridgeResponse(pydantic.BaseModel):
    status: str
    pages: list[str]


class BridgePayload(pydantic.BaseModel):
    secret: str
    message: str
    sender: str


class MessagePayload(pydantic.BaseModel):
    class MessageAttachmentPayload(pydantic.BaseModel):
        url: str
        proxy_url: str
        filename: str
        size: int
        width: int
        height: int
        content_type: str

    message_id: int
    author: str
    avatar: str
    content: str
    clean_content: str
    at: float
    attachments: list[MessageAttachmentPayload] = []
    reply_to: Optional["MessagePayload"] = None


class DiscordBridge(niobot.Module):
    """Bridge between the mirror and the discord server."""

    def __init__(self, bot: niobot.NioBot):
        super().__init__(bot)
        self._db_ready = False
        self.log = logging.getLogger("philip.discord_bridge")
        self.config = config["philip"].get("bridge", {})
        assert isinstance(self.config, dict), "Invalid bridge config. Must be a dict"

        self.token = self.config.get("token", None)
        assert self.token, "No token set for bridge - unable to proceed."
        self.channel_id = self.config.get("channel_id", "!WrLNqENUnEZvLJiHsu:nexy7574.co.uk")
        self.webhook_url = self.config.get("webhook_url", None)
        if not self.webhook_url:
            self.log.warning("No webhook URL set for bridge - the other end will be ugly.")

        self.websocket_endpoint = self.config.get("websocket_endpoint")
        if not self.websocket_endpoint:
            self.websocket_endpoint = "wss://droplet.nexy7574.co.uk/jimmy/bridge/recv"

        self.avatar_cache_path = self.config.get("avatar_cache_path")
        if not self.avatar_cache_path:
            self.avatar_cache_path = Path.home() / ".cache" / "philip" / "avatars.db"
            self.avatar_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.avatar_cache_path.touch(exist_ok=True)
        else:
            self.avatar_cache_path = Path(self.avatar_cache_path).resolve()
            self.avatar_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.avatar_cache_path.touch(exist_ok=True)

        self.last_message: Optional[MessagePayload] = None
        self.message_cache: list[
            dict[
                typing.Literal["discord", "matrix"],
                MessagePayload | nio.RoomMessage | nio.RoomSendResponse
            ]
        ] = []

    async def _init_db(self):
        if self._db_ready:
            return
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS avatars (
                    user_id TEXT PRIMARY KEY,
                    mxc TEXT,
                    expires INTEGER
                )
                """
            )
            await db.commit()
        self._db_ready = True

    @staticmethod
    def make_image_round(path: Path) -> Path:
        img = PIL.Image.open(path)
        img = img.convert("RGBA")
        mask = PIL.Image.new("L", img.size, 0)

        draw = PIL.ImageDraw.Draw(mask)
        draw.ellipse((0, 0) + img.size, fill=255)

        img.putalpha(mask)
        img.thumbnail((16, 16), PIL.Image.Resampling.LANCZOS, 3)
        img.save(path)
        return path

    async def get_avatar_from_cache(self, user_id: str) -> Optional[str]:
        await self._init_db()
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            async with db.execute(
                    "SELECT (mxc, expires) FROM avatars WHERE user_id = ?",
                    (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    url, expires = row
                    if expires > time.time():
                        return url
                    else:
                        self.log.debug("Avatar for %s expired.", user_id)
                else:
                    self.log.debug("No avatar for %s found.", user_id)

    async def download_avatar(self, user_id: str, url: str) -> Optional[str]:
        await self._init_db()
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            async with httpx.AsyncClient(headers={"User-Agent": niobot.__user_agent__}) as client:
                response = await client.get(url)
                if response.status_code != 200:
                    self.log.warning("Failed to download avatar for %s: %s", user_id, response.status_code)
                    return ""
                filename = response.request.url.path.split("/")[-1]
                with tempfile.NamedTemporaryFile(suffix=filename, mode="wb") as f:
                    f.write(response.content)
                    f.flush()
                    self.make_image_round(Path(f.name))
                    attachment = await niobot.ImageAttachment.from_file(
                        io.BytesIO(response.content),
                        filename,
                        generate_blurhash=False
                    )
                    await attachment.upload(self.bot, False)
                await db.execute(
                    """
                    INSERT INTO avatars
                    VALUES (?, ?, ?)
                    """
                    ,
                    (user_id, attachment.url, round(time.time() + 806400))
                )
                await db.commit()
        return attachment.url

    def should_prepend_username(self, payload: MessagePayload) -> bool:
        if self.last_message:
            if payload.at - self.last_message.at < 300:
                if payload.author == self.last_message.author:
                    if payload.content:
                        return False
        return True

    async def poll_loop(self):
        if not self.bot.is_ready.is_set():
            await self.bot.is_ready.wait()
        room = self.bot.rooms[self.channel_id]

        while True:
            async with httpx.AsyncClient() as client:
                async for ws in websockets.client.connect(
                    self.websocket_endpoint,
                    logger=self.log,
                    extra_headers={
                        "secret": self.token,
                    },
                    user_agent_header="%s Philip" % niobot.__user_agent__,
                ):
                    self.log.info("Connected to jimmy bridge.")
                    async for payload in ws:
                        self.log.debug("Received message from bridge: %s", payload)
                        try:
                            payload_json = json.loads(payload)
                            payload = MessagePayload(**payload_json)
                        except json.JSONDecodeError as e:
                            self.log.error("Invalid JSON payload: %s", e, exc_info=True)
                            continue
                        except pydantic.ValidationError as e:
                            self.log.error("Invalid message payload: %s", e, exc_info=True)
                            continue

                        if payload.author == "Jimmy Savile#3762":
                            self.log.debug("Ignoring discord message from myself.")
                            continue

                        reply_to = None
                        if payload.reply_to:
                            for cached_message in self.message_cache:
                                if cached_message["discord"].message_id == payload.reply_to.message_id:
                                    reply_to = cached_message["matrix"]
                                    break

                        if payload.content:
                            new_content = ""
                            if self.should_prepend_username(payload):
                                new_content += f"**@{payload.author}:**\n"

                            pre_rendered = await self.bot._markdown_to_html(payload.clean_content)
                            new_content += "<blockquote>%s</blockquote>" % pre_rendered
                        elif payload.attachments:
                            new_content = "@%s sent %d attachments." % (payload.author, len(payload.attachments))
                        else:
                            new_content = "@%s sent no content." % payload.author

                        root = await self.bot.send_message(
                            room,
                            new_content,
                            reply_to=reply_to,
                            message_type="m.text",
                            clean_mentions=False
                        )
                        self.message_cache.append(
                            {
                                "discord": payload,
                                "matrix": self.bot.get_cached_message(root.event_id) or root
                            }
                        )
                        for attachment in payload.attachments:
                            with tempfile.NamedTemporaryFile(
                                "wb",
                                suffix=attachment.filename,
                            ) as temp_file_fd:
                                temp_file = Path(temp_file_fd.name)
                                response = await client.get(attachment.url)
                                if response.status_code == 404:
                                    response = await client.get(attachment.proxy_url)

                                if response.status_code != 200:
                                    self.log.warning("Failed to download attachment: %s", response.status_code)
                                    continue
                                temp_file_fd.write(response.content)
                                temp_file_fd.flush()
                                temp_file_fd.seek(0)

                                discovered = niobot.which(temp_file)
                                match discovered:
                                    case niobot.VideoAttachment:
                                        # Do some additional processing.
                                        first_frame_bytes = await niobot.run_blocking(
                                            niobot.first_frame,
                                            temp_file,
                                            "webp"
                                        )
                                        first_frame_bio = io.BytesIO(first_frame_bytes)
                                        thumbnail_attachment = await niobot.ImageAttachment.from_file(
                                            first_frame_bio,
                                            attachment.filename + "-thumbnail.webp",
                                            height=attachment.height,
                                            width=attachment.width
                                        )
                                        file_attachment = await discovered.from_file(
                                            temp_file,
                                            height=attachment.height,
                                            width=attachment.width,
                                            thumbnail=thumbnail_attachment
                                        )
                                    case niobot.ImageAttachment:
                                        file_attachment = await discovered.from_file(
                                            temp_file,
                                            height=attachment.height,
                                            width=attachment.width
                                        )
                                    case niobot.AudioAttachment:
                                        file_attachment = await discovered.from_file(
                                            temp_file
                                        )
                                    case niobot.FileAttachment:
                                        file_attachment = await discovered.from_file(
                                            temp_file
                                        )
                                await self.bot.send_message(
                                    room,
                                    file=file_attachment,
                                    reply_to=root.event_id,
                                    message_type=file_attachment.type.value
                                )

    async def on_message(self, room: nio.MatrixRoom, message: nio.RoomMessageText | nio.RoomMessageMedia):
        if self.bot.is_old(message):
            return

        if room.room_id != self.channel_id:
            return

        if message.body.startswith((self.bot.command_prefix, "!", "?", ".", "-")):
            return

        if message.sender == self.bot.user_id:
            return

        payload = BridgePayload(
            secret=self.token,
            message=message.body,
            sender=message.sender
        )
        if isinstance(message, nio.RoomMessageMedia):
            payload.message = await self.bot.mxc_to_http(message.url)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.websocket_endpoint,
                json=payload.model_dump()
            )
            if response.status_code == 400:
                self.log.warning("Message %s was too long to send to discord.", message.event_id)
                data = await response.json()
                if data["detail"] == "Message too long.":
                    await self.bot.add_reaction(room, message, "\N{PRINTER}\N{VARIATION SELECTOR-16}")
            elif response.status_code != 201:
                self.log.error(
                    "Error while sending message (%s) to discord bridge (%d): %s",
                    message.event_id,
                    response.status_code,
                    response.text
                )
                await self.bot.add_reaction(room, message, "\N{CROSS MARK}")
                return
            else:
                self.log.info("Message %s sent to discord bridge", message.event_id)
