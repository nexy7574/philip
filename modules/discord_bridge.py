import asyncio
import io
import json
import logging
import subprocess
import tempfile
import time
import typing
import re
from pathlib import Path

import PIL.Image
import PIL.ImageDraw
import nio
import pydantic
import niobot
import websockets.client
import httpx
import aiosqlite
from bs4 import BeautifulSoup

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
        width: Optional[int] = None
        height: Optional[int] = None
        content_type: str
        ATTACHMENT: Optional[typing.Any] = None

    event_type: Optional[str] = "create"
    message_id: int
    author: str
    is_automated: bool = False
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
        self.bot.add_event_callback(
            self.on_message,
            (nio.RoomMessageText, nio.RoomMessageMedia)
        )
        self.task: Optional[asyncio.Task] = None

    @niobot.event("ready")
    async def on_ready(self, _):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.poll_loop_wrapper())

    def __teardown__(self):
        if self.task:
            self.task.cancel()
        super().__teardown__()

    async def _init_db(self):
        if self._db_ready:
            return
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS image_cache (
                    http_url TEXT PRIMARY KEY,
                    mxc_url TEXT,
                    etag TEXT DEFAULT NULL,
                    last_modified TEXT DEFAULT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bridge_messages (
                    matrix_id TEXT PRIMARY KEY,
                    discord_id INTEGER,
                    message_type TEXT
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

    async def get_image_from_cache(self, http: str, *, round: bool = False, encrypted: bool = False) -> Optional[str]:
        """
        Fetches an image from the cache, or if not found, uploads it and returns the mxc.

        :param http: The HTTP URL to fetch
        :param round: Whether to circle the image
        :param encrypted: Whether the image is encrypted
        :return: The resolved MXC URL
        """
        await self._init_db()
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            self.log.debug("Fetching cached image for %s", http)
            async with db.execute(
                """
                SELECT mxc_url FROM image_cache WHERE http_url = ?
                """,
                (http,)
            ) as cursor:
                row = await cursor.fetchone()
                self.log.debug("Row: %r", row)
                if row:
                    return row[0]

                async with httpx.AsyncClient() as client:
                    response = await client.get(http)
                    if response.status_code != 200:
                        self.log.warning("Failed to fetch avatar: %s", response.status_code)
                        return None
                    file_name = response.request.url.path.split("/")[-1]
                    with tempfile.NamedTemporaryFile(suffix=file_name) as fd:
                        fd.write(response.content)
                        fd.flush()
                        fd.seek(0)
                        fd_path = Path(fd.name)
                        if round:
                            fd_path = self.make_image_round(fd_path)
                        attachment = await niobot.which(fd_path).from_file(fd_path)
                        await attachment.upload(self.bot, encrypted=encrypted)
                        await db.execute(
                            """
                            INSERT INTO image_cache (http_url, mxc_url) VALUES (?, ?)
                            """,
                            (http, attachment.url)
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

    async def poll_loop_wrapper(self):
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await self.poll_loop(client)
                except asyncio.CancelledError:
                    raise
                except websockets.exceptions.ConnectionClosedError:
                    self.log.warning("Connection to jimmy bridge closed. Retrying in 5 seconds.")
                    await asyncio.sleep(5)
                except Exception as e:
                    self.log.error("Error in poll loop: %s", e, exc_info=True)
                    await asyncio.sleep(5)

    async def add_message_to_db(self, matrix_id: str, discord_id: int, message_type: str = "content"):
        """
        Adds a message to the database.

        :param matrix_id: The Matrix event ID
        :param discord_id: The discord message ID
        :param message_type: The message type (content or attachment)
        :return:
        """
        await self._init_db()
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            await db.execute(
                """
                INSERT INTO bridge_messages (matrix_id, discord_id, message_type) VALUES (?, ?, ?)
                """,
                (matrix_id, discord_id, message_type)
            )
            await db.commit()

    async def get_message_from_db(
            self,
            matrix_id: str = None,
            discord_id: int = None,
    ) -> Optional[dict]:
        if not any((matrix_id, discord_id)):
            raise ValueError("Must specify either matrix_id or discord_id")
        await self._init_db()
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            if matrix_id:
                query = "SELECT matrix_id, discord_id, message_type FROM bridge_messages WHERE matrix_id = ?"
                args = (matrix_id,)
            else:
                query = "SELECT matrix_id, discord_id, message_type FROM bridge_messages WHERE discord_id = ?"
                args = (discord_id,)
            async with db.execute(query, args) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "matrix_id": row[0],
                        "discord_id": row[1],
                        "message_type": row[2]
                    }
                else:
                    return None

    async def delete_message_from_db(
            self,
            matrix_id: str = None,
            discord_id: int = None,
    ):
        if not matrix_id and not discord_id:
            raise ValueError("Must specify either matrix_id or discord_id")
        await self._init_db()
        async with aiosqlite.connect(self.avatar_cache_path) as db:
            if matrix_id:
                query = "DELETE FROM bridge_messages WHERE matrix_id = ?"
                args = (matrix_id,)
            else:
                query = "DELETE FROM bridge_messages WHERE discord_id = ?"
                args = (discord_id,)
            await db.execute(query, args)
            await db.commit()

    async def download_gif(self, url: str) -> Path:
        async with httpx.AsyncClient(
                follow_redirects=True,
                headers={
                    # Mask as twitterbot
                    "User-Agent": "Twitterbot/1.0",
                    "Finagle-Ctx-Com.twitter.finagle.retries": "0"
                }
        ) as client:
            response = await client.get(url)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                for meta in soup.find_all("meta"):
                    self.log.debug(meta)
                    if meta.get("property") == "twitter:image":
                        image_url = meta.get("content")
                        break
                else:
                    raise RuntimeError("Could not find twitter:image meta tag.")

                if image_url:
                    self.log.debug("Found GIF URL: %s", image_url)
                    response = await client.get(image_url)
                    if response.status_code == 200:
                        with tempfile.NamedTemporaryFile(
                                "wb",
                                suffix=".gif"
                        ) as temp_file_fd:
                            temp_file = Path(temp_file_fd.name)
                            temp_file_fd.write(response.content)
                            temp_file_fd.flush()
                            temp_file_fd.seek(0)
                            with tempfile.NamedTemporaryFile(
                                "wb",
                                suffix=".webp"
                            ) as webp_temp_file_fd:
                                webp_temp_file = Path(webp_temp_file_fd.name)
                                await niobot.run_blocking(
                                    subprocess.run,
                                    (
                                        "convert",
                                        str(temp_file.absolute()),
                                        str(webp_temp_file.absolute())
                                    ),
                                    capture_output=True,
                                    check=True
                                )
                                webp_temp_file_fd.flush()
                                webp_temp_file_fd.seek(0)
                                return webp_temp_file

    async def poll_loop(self, client: httpx.AsyncClient):
        if not self.bot.is_ready.is_set():
            await self.bot.is_ready.wait()
        room = self.bot.rooms[self.channel_id]

        while True:
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
                        if payload_json.get("status") == "ping":
                            self.log.debug("Got PING from bridge, ignoring.")
                            continue
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
                    elif payload.is_automated:
                        self.log.debug("Ignoring discord message from webhook or bot.")
                        continue

                    reply_to = None
                    if payload.reply_to:
                        if db := await self.get_message_from_db(discord_id=payload.reply_to.message_id):
                            reply_to = db
                        else:
                            self.log.debug("Could not find message reply.")
                    else:
                        self.log.debug("Message had no reply.")

                    if gif_match := re.match(
                        r"https://tenor\.com/.+",
                        payload.content or ''
                    ):
                        self.log.debug("Found tenor GIF: %s", gif_match)
                        # noinspection PyBroadException
                        try:
                            gif = await self.download_gif(payload.content)
                        except Exception:
                            self.log.error("Failed to download gif.", exc_info=True)
                            gif = None
                        if gif:
                            self.log.debug("Uploading GIF to matrix.")
                            gif_attachment = await niobot.ImageAttachment.from_file(
                                gif,
                            )
                            await gif_attachment.upload(self.bot)
                            payload.content = None
                            payload.attachments.append(
                                MessagePayload.MessageAttachmentPayload(
                                    url=gif_attachment.url,
                                    proxy_url=gif_attachment.url,
                                    filename=gif_attachment.file_name,
                                    size=gif_attachment.size,
                                    width=gif_attachment.info['w'],
                                    height=gif_attachment.info['h'],
                                    content_type=gif_attachment.mime_type,
                                    ATTACHMENT=gif_attachment
                                )
                            )

                    if payload.content:
                        new_content = ""
                        if self.should_prepend_username(payload):
                            avatar = await self.get_image_from_cache(payload.avatar, round=True)
                            if avatar:
                                avatar = '<img src="%s" width="16px" height="16px"> ' % avatar
                            else:
                                avatar = ""
                            new_content += f"**{avatar}{payload.author}:**\n"

                        body = f"**{payload.author}:**\n{payload.clean_content}"
                        pre_rendered = await self.bot._markdown_to_html(payload.clean_content)
                        new_content += "<blockquote>%s</blockquote>" % pre_rendered
                    elif payload.attachments:
                        new_content = body = "@%s sent %d attachments." % (payload.author, len(payload.attachments))
                    else:
                        new_content = body = "@%s sent no content." % payload.author

                    self.log.info("Rendered content for matrix: %r", new_content)

                    self.log.debug("Sending message to %r", room)
                    try:
                        root = await self.bot.send_message(
                            room,
                            new_content,
                            reply_to=reply_to,
                            message_type="m.text",
                            clean_mentions=False,
                            override={
                                "body": body
                            }
                        )
                        cache = await self.get_message_from_db(discord_id=payload.message_id)
                        if cache:
                            await self.delete_message_from_db(discord_id=payload.message_id)
                        await self.add_message_to_db(
                            root.event_id,
                            payload.message_id,
                        )
                    except niobot.MessageException as e:
                        self.log.error("Failed to send bridge message to matrix: %r", e, exc_info=True)
                        continue

                    for attachment in payload.attachments:
                        self.log.info("Processing attachment %s", attachment)
                        if attachment.url.startswith("mxc://"):
                            # Already uploaded. All we need to do is send it.
                            await self.bot.send_message(
                                room,
                                file=attachment.ATTACHMENT,
                                reply_to=root.event_id,
                            )
                            continue

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
                                    await thumbnail_attachment.upload(self.bot)
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
                                    if temp_file.stat().st_size > 56000:
                                        thumbnail = await niobot.run_blocking(
                                            file_attachment.thumbnailify_image,
                                            temp_file,
                                        )
                                        with tempfile.NamedTemporaryFile(
                                            "wb",
                                            suffix="-thumbnail.webp"
                                        ) as thumbnail_temp_file_fd:
                                            thumbnail_temp_file = Path(thumbnail_temp_file_fd.name)
                                            thumbnail.save(thumbnail_temp_file_fd, format="webp")
                                            thumbnail_temp_file_fd.flush()
                                            thumbnail_temp_file_fd.seek(0)
                                            thumbnail_attachment = await niobot.ImageAttachment.from_file(
                                                thumbnail_temp_file,
                                                attachment.filename + "-thumbnail.webp",
                                                height=attachment.height,
                                                width=attachment.width
                                            )
                                            await thumbnail_attachment.upload(self.bot)
                                            file_attachment.thumbnail = thumbnail_attachment

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

        self.log.debug("Got matrix message: %r in room %r", message, room)

        payload = BridgePayload(
            secret=self.token,
            message=message.body,
            sender=message.sender
        )
        if isinstance(message, nio.RoomMessageMedia):
            filename = message.body
            file_url = await self.bot.mxc_to_http(message.url)
            payload.message = "[{}]({})".format(filename, file_url)

        async with httpx.AsyncClient() as client:
            if self.webhook_url:
                avatar = None
                profile = await self.bot.get_profile(message.sender)
                if isinstance(profile, nio.ProfileGetResponse):
                    if profile.avatar_url:
                        avatar = await self.bot.mxc_to_http(profile.avatar_url)
                body = {
                    "content": payload.message,
                    "username": payload.sender[:32],
                    "allowed_mentions": {
                        "parse": [
                            "users"
                        ],
                        "replied_user": True
                    }
                }
                if avatar:
                    body["avatar_url"] = avatar
                response = await client.post(
                    self.webhook_url,
                    params={"wait": self.config.get("webhook_wait", False)},
                    json=body
                )
                if response.status_code in range(200, 300):
                    self.log.info("Message %s sent to discord bridge via webhook", message.event_id)
                    return
                else:
                    self.log.warning(
                        "Failed to bridge message %s using webhook (%d). will fall back to websocket.",
                        message.event_id,
                        response.status_code
                    )
            response = await client.post(
                "https://droplet.nexy7574.co.uk/jimmy/bridge",
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
