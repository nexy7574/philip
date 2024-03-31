import asyncio
import io
import json
import logging
import re
import tempfile
import typing
from pathlib import Path

import PIL.features
import PIL.Image
import PIL.ImageDraw
import time
import nio
import pydantic
import niobot
import websockets.client
import httpx
import aiosqlite

from util import config, DiscordAPI, JimmyAPI
from typing import Optional, Union


class BridgeResponse(pydantic.BaseModel):
    status: str
    pages: list[str]


class BridgePayload(pydantic.BaseModel):
    secret: str
    message: str
    sender: str
    room: str


class FakeMessagePayload(pydantic.BaseModel):
    author: str
    at: float


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

        self.jimmy: JimmyAPI = JimmyAPI()
        self.discord: DiscordAPI = DiscordAPI()

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
            dict[typing.Literal["discord", "matrix"], MessagePayload | nio.RoomMessage | nio.RoomSendResponse]
        ] = []
        self.bot.add_event_callback(self.on_message, (nio.RoomMessageText, nio.RoomMessageMedia))
        # noinspection PyTypeChecker
        self.bot.add_event_callback(self.on_redaction, (nio.RedactionEvent,))
        self.task: Optional[asyncio.Task] = None
        self.bridge_lock = asyncio.Lock()

        self.bind_cache = {}

        self.matrix_to_discord: dict[str, int] = {}
        self.discord_to_matrix: dict[int, str] = {}

    @property
    def token(self) -> str | None:
        return self.jimmy.token

    @property
    def channel_id(self) -> int:
        return self.jimmy.default_channel_id

    @property
    def guild_id(self) -> int:
        return self.jimmy.guild_id

    @property
    def websocket_endpoint(self) -> str:
        return self.jimmy.websocket_endpoint

    @property
    def webhook_url(self) -> str | None:
        return self.jimmy.config.get("webhook_url")

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

    async def get_discord_user(self, user_id: int) -> Optional[dict[str, Union[None, str, float]]]:
        """
        Fetches a user from the discord API.
        This function is used to fetch user information for the webhook.

        This function will check the memory cache for information first.
        Information is cached for around a day, before it is soft-expired.

        :param user_id: the user's ID to fetch.
        :return: {"username": "...", "avatar": "...", "expires": time.time() + 86400}
        """
        if user_id in self.bind_cache:
            if self.bind_cache[user_id]["expires"] > time.time():
                self.log.debug("Returning cached user info for %d", user_id)
                return self.bind_cache[user_id]
            else:
                self.log.debug("Cached user info for %d has expired.", user_id)
        else:
            self.log.debug("No cached user info for %d", user_id)
        AVATAR_URL = "https://cdn.discordapp.com/avatars/%d/%s.webp?size=256"
        if self.guild_id:
            url = "/guilds/%d/members/%d" % (self.guild_id, user_id)
        else:
            url = "/users/%d" % user_id
        async with self.discord.session(include_token=True) as client:
            response = await client.get(url, headers={"Authorization": "Bot " + self.token})
            if response.status_code == 200:
                data = response.json()
                user_data = data.get("user", data)
                display_name = data.get("nick") or user_data["username"]
                self.log.debug("Found user %r, caching.", display_name)
                avatar = None
                if data.get("avatar"):
                    avatar = AVATAR_URL % (user_id, data["avatar"])
                elif user_data.get("avatar"):
                    avatar = AVATAR_URL % (user_id, user_data["avatar"])
                return {"username": display_name, "avatar": avatar, "expires": time.time() + 86400}

    async def get_bound_account(self, sender: str) -> Optional[int]:
        """
        Fetches the user's bound discord account ID from the Jimmy v1 API.

        :param sender: The matrix sender account
        :return: The discord ID, if available.
        """
        if sender.startswith("@"):
            sender = sender[1:]

        bind = await self.jimmy.get_bridge_bind(sender)
        if bind:
            return bind["discord"]

    async def get_image_from_cache(
        self, http: str, *, make_round: bool = False, encrypted: bool = False
    ) -> Optional[str]:
        """
        Fetches an image from the cache, or if not found, uploads it and returns the mxc.

        :param http: The HTTP URL to fetch
        :param make_round: Whether to circle the image
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
                (http,),
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
                        if make_round:
                            fd_path = self.make_image_round(fd_path)
                        attachment = await niobot.which(fd_path).from_file(fd_path)
                        await attachment.upload(self.bot, encrypted=encrypted)
                        await db.execute(
                            """
                            INSERT INTO image_cache (http_url, mxc_url) VALUES (?, ?)
                            """,
                            (http, attachment.url),
                        )
                        await db.commit()
                        return attachment.url

    def should_prepend_username(self, payload: MessagePayload) -> bool:
        if self.last_message:
            self.log.debug("Have last message: %r", self.last_message)
            if payload.at - self.last_message.at < 300:
                self.log.debug("Last message was within 5 minutes.")
                if payload.author == self.last_message.author:
                    self.log.debug("Last message was from the same author.")
                    if payload.content:
                        self.log.debug("Message has content - should not include author.")
                        return False
        self.log.debug("For some reason, should include author")
        return True

    async def poll_loop_wrapper(self):
        async with self.jimmy.session() as client:
            while True:
                try:
                    _exit = await self.poll_loop(client)
                    if _exit:
                        self.log.critical("Notified to exit bridge poll loop.")
                        break
                except asyncio.CancelledError:
                    raise
                except websockets.exceptions.ConnectionClosedError:
                    self.log.warning("Connection to jimmy bridge closed. Retrying in 5 seconds.")
                    await asyncio.sleep(5)
                except Exception as e:
                    self.log.error("Error in poll loop: %s", e, exc_info=True)
                    await asyncio.sleep(5)

    def convert_image(self, path: Path, quality: int = 90, speed: int = 0) -> Path:
        speed = 6 - speed
        with tempfile.NamedTemporaryFile("wb", suffix=path.with_suffix(".webp").name, delete=False) as temp_fd:
            img = PIL.Image.open(path)
            kwargs = {"format": "webp", "quality": quality, "method": speed}
            if getattr(img, "is_animated", False) and PIL.features.check("webp_anim"):
                kwargs["save_all"] = True
            self.log.info("Converting image %r to webp with kwargs %r", path, kwargs)
            img.save(temp_fd, **kwargs)
            temp_fd.flush()
            return Path(temp_fd.name)

    async def generate_matrix_content(self, payload: MessagePayload, force_author: bool = None):
        included_author = False
        if payload.content:
            new_content = ""
            if self.should_prepend_username(payload):
                included_author = True
                avatar = await self.get_image_from_cache(payload.avatar, make_round=True)
                if avatar:
                    avatar = '<img src="%s" width="16px" height="16px" alt="[\U0001f464]"> ' % avatar
                else:
                    avatar = ""
                new_content += f"**{avatar}{payload.author}:**\n"
                self.log.debug("Prepending username.")
            else:
                self.log.debug("Not prepending username.")

            body = f"**{payload.author}:**\n{payload.clean_content}"
            new_content = await self.bot._markdown_to_html(new_content + payload.clean_content)

            # Now need to replace all instances of ~~$content$~~ with <del>$content$</del>
            def convert_tag(_match: typing.Match[str]) -> str:
                return f"<del>{_match.group(3)}</del>"

            new_content = re.sub(r"(?P<start>((?!\\)~){2})([^~]+)(?P<end>((?!\\)~){2})", convert_tag, new_content)

        elif payload.attachments:
            new_content = body = "@%s sent %d attachments." % (payload.author, len(payload.attachments))
        else:
            new_content = body = "@%s sent no content." % payload.author
        return new_content, body, included_author

    async def poll_loop(self, client: httpx.AsyncClient):
        if not self.bot.is_ready.is_set():
            await self.bot.is_ready.wait()
        room = self.bot.rooms.get(self.channel_id)
        if not room:
            self.log.warning("No room for bridge!")
            return True

        while True:
            async for ws in websockets.client.connect(
                self.websocket_endpoint + "?secret=" + self.jimmy.token,
                logger=self.log,
                user_agent_header="%s Philip" % niobot.__user_agent__,
            ):
                self.log.info("Connected to jimmy bridge.")
                async for payload in ws:
                    self.log.debug("Received message from bridge: %s", payload)
                    try:
                        payload_json = json.loads(payload)
                        if payload_json.get("status") == "ping":
                            self.log.debug("Got PING from bridge.")
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
                        if payload.reply_to.message_id in self.discord_to_matrix:
                            reply_to = self.discord_to_matrix[payload.reply_to.message_id]
                        else:
                            self.log.warning("Unknown reply_to: %r", payload.reply_to)
                    else:
                        self.log.debug("Message had no reply.")

                    if payload.event_type == "redact":
                        self.log.debug("Redacting message %r", payload.message_id)
                        await self.redact_matrix_message(payload.message_id)
                        continue
                    elif payload.event_type == "edit":
                        self.log.debug("Editing message %r", payload.message_id)
                        included_author = False
                        original_event = await self.bot.room_get_event(
                            self.channel_id, self.discord_to_matrix[payload.message_id]
                        )
                        if isinstance(original_event, nio.RoomGetEventResponse):
                            original_event = original_event.event
                            source = original_event.source
                            if "nexus.i-am.bridge.author" in source:
                                included_author = source["nexus.i-am.bridge.author"] == "true"
                        new_content, body, included_author = await self.generate_matrix_content(
                            payload, included_author
                        )
                        await self.edit_matrix_message(
                            payload.message_id,
                            new_content,
                            message_type="m.text",
                            override={"body": body, "nexus.i-am.bridge.author": "true" if included_author else "false"},
                        )
                        continue
                    elif payload.event_type != "create":
                        self.log.warning("Unknown event type: %r", payload.event_type)
                        continue

                    new_content, body, included_author = await self.generate_matrix_content(payload)
                    self.log.debug("Rendered content for matrix: %r", new_content)

                    self.log.debug("Sending message to %r", room)
                    async with self.bridge_lock:
                        try:
                            root = await self.bot.send_message(
                                room,
                                new_content,
                                reply_to=reply_to,
                                message_type="m.text",
                                clean_mentions=False,
                                override={
                                    "body": body,
                                    "nexus.i-am.bridge.author": "true" if included_author else "false",
                                },
                            )
                            self.discord_to_matrix[payload.message_id] = root.event_id
                            self.last_message = payload
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
                                            niobot.first_frame, temp_file, "webp"
                                        )
                                        first_frame_bio = io.BytesIO(first_frame_bytes)
                                        thumbnail_attachment = await niobot.ImageAttachment.from_file(
                                            first_frame_bio,
                                            attachment.filename + "-thumbnail.webp",
                                            height=attachment.height,
                                            width=attachment.width,
                                        )
                                        await thumbnail_attachment.upload(self.bot)
                                        file_attachment = await discovered.from_file(
                                            temp_file,
                                            height=attachment.height,
                                            width=attachment.width,
                                            thumbnail=thumbnail_attachment,
                                        )
                                    case niobot.ImageAttachment:
                                        # Convert it to webp.
                                        if attachment.content_type != "image/gif":
                                            new_file = await niobot.run_blocking(
                                                self.convert_image, temp_file, speed=0, quality=80
                                            )
                                        else:
                                            new_file = temp_file
                                        file_attachment = await discovered.from_file(
                                            new_file, height=attachment.height, width=attachment.width
                                        )

                                    case niobot.AudioAttachment:
                                        file_attachment = await discovered.from_file(temp_file)
                                    case niobot.FileAttachment:
                                        file_attachment = await discovered.from_file(temp_file)
                                await self.bot.send_message(
                                    room,
                                    file=file_attachment,
                                    reply_to=root.event_id,
                                    message_type=file_attachment.type.value,
                                )

    async def edit_webhook_message(
        self, client: httpx.AsyncClient, new_content: str, *, original_event_id: str, new_event_id: str
    ):
        response = await client.patch(
            self.webhook_url + "/messages/" + str(self.matrix_to_discord[original_event_id]),
            json={"content": new_content},
        )
        if response.status_code not in range(200, 300):
            self.log.warning(
                "Failed to edit message %s: %d - %s", original_event_id, response.status_code, response.text
            )
        self.matrix_to_discord[new_event_id] = self.matrix_to_discord[original_event_id]
        return

    async def redact_webhook_message(self, client: httpx.AsyncClient, event_id: str):
        """Redacts a discord message"""
        response = await client.delete(self.webhook_url + "/messages/" + str(self.matrix_to_discord[event_id]))
        if response.status_code not in range(200, 300):
            self.log.warning("Failed to redact message %s: %d - %s", event_id, response.status_code, response.text)
        del self.matrix_to_discord[event_id]

    async def redact_matrix_message(self, message_id: int):
        """Redacts a matrix sent from discord to matrix"""
        if message_id in self.discord_to_matrix:
            self.log.debug("Deleting matrix message %r", message_id)
            await self.bot.room_redact(self.channel_id, self.discord_to_matrix[message_id])
            del self.discord_to_matrix[message_id]

    async def edit_matrix_message(self, message_id: int, new_content: str, **kwargs):
        """Edits a matrix message sent from discord to matrix"""
        if message_id in self.discord_to_matrix:
            self.log.debug("Editing matrix message %r", message_id)
            await self.bot.edit_message(self.channel_id, self.discord_to_matrix[message_id], new_content, **kwargs)

    async def on_message(self, room, message):
        try:
            return await self.real_on_message(room, message)
        except Exception as e:
            self.log.error("Error in on_message: %s", e, exc_info=True)

    async def on_redaction(self, room: nio.MatrixRoom, redaction: nio.RedactionEvent):
        if self.bot.is_old(redaction):
            return
        if room.room_id != self.channel_id:
            return
        try:
            return await self.real_on_redaction(redaction)
        except Exception as e:
            self.log.error("Error in on_redaction: %s", e, exc_info=True)

    async def real_on_redaction(self, redaction: nio.RedactionEvent):
        if redaction.redacts in self.matrix_to_discord:
            async with httpx.AsyncClient() as client:
                self.log.debug("Redacting message %s from discord.", redaction.redacts)
                if redaction.reason:
                    self.log.debug("Redacting %s via edit", redaction.redacts)
                    await self.edit_webhook_message(
                        client,
                        f"*Message was redacted: {redaction.reason[:1900]}*",
                        original_event_id=redaction.redacts,
                        new_event_id=redaction.event_id,
                    )
                else:
                    self.log.debug("Redacting %s via delete", redaction.redacts)
                    await self.redact_webhook_message(client, redaction.redacts)
        else:
            self.log.debug("Ignoring redaction %s", redaction.redacts)

    async def real_on_message(self, room: nio.MatrixRoom, message: nio.RoomMessageText | nio.RoomMessageMedia):
        if self.bot.is_old(message):
            return

        if room.room_id != self.channel_id:
            return

        if message.body.startswith((self.bot.command_prefix, "!", "?", ".", "-")):
            return

        if message.sender == self.bot.user_id:
            return

        self.log.debug("Got matrix message: %r in room %r", message, room)

        payload = BridgePayload(secret=self.token, message=message.body, sender=message.sender, room=room.room_id)
        if isinstance(message, nio.RoomMessageMedia):
            content_type = message.source["content"].get("info", {}).get("mimetype", "")
            filename = message.body
            file_url = await self.bot.mxc_to_http(message.url)
            if content_type.startswith("video/"):
                file_url = "https://embeds.video/" + file_url
            payload.message = "[{}]({})".format(filename, file_url)

        self.log.debug("checking if %s has a discord bound account", message.sender)
        avatar = None
        bound_account = await self.get_bound_account(message.sender)
        if bound_account:
            self.log.debug("Found bound discord account: %s=%d", message.sender, bound_account)
            user_data = await self.get_discord_user(bound_account)
            if user_data:
                self.log.debug("Got discord user data for %s", message.sender)
                payload.sender = user_data["username"]
                if user_data["avatar"]:
                    avatar = user_data["avatar"]
        else:
            self.log.debug("No bound discord account for %s", message.sender)

        async with self.discord.session(None, True) as client:
            if "m.new_content" in message.source["content"]:
                new_content = message.source["content"]["m.new_content"]["body"]
                original_event_id = message.source["content"]["m.relates_to"]["event_id"]
                if original_event_id in self.matrix_to_discord:
                    return await self.edit_webhook_message(
                        client, new_content, original_event_id=original_event_id, new_event_id=message.event_id
                    )
                else:
                    self.log.warning("Unrecognised replacement event: %s", original_event_id)
            if self.webhook_url:
                self.log.debug("Have a registered webhook URL. Using it.")
                try:
                    if avatar is None:
                        self.log.debug("Fetching %s avatar from matrix.", message.sender)
                        profile = await self.bot.get_profile(message.sender)
                        if isinstance(profile, nio.ProfileGetResponse):
                            if profile.avatar_url:
                                self.log.debug("Fetching avatar from %s", profile.avatar_url)
                                avatar = await self.bot.mxc_to_http(profile.avatar_url)
                            else:
                                self.log.debug("No avatar found.")
                        else:
                            self.log.warning("Failed to fetch profile for %s", message.sender)
                    else:
                        self.log.debug("Already have an avatar")
                except Exception as e:
                    self.log.error("Error while fetching avatar: %s", e, exc_info=True)
                self.log.debug("Preparing body")
                body = {
                    "content": payload.message,
                    "username": payload.sender[:32],
                    "allowed_mentions": {"parse": ["users"], "replied_user": True},
                }
                if avatar:
                    body["avatar_url"] = avatar
                self.log.debug("Body: %r", body)
                self.log.debug("Sending message to discord.")
                response = await client.post(
                    self.webhook_url, params={"wait": self.config.get("webhook_wait", False)}, json=body
                )
                if response.status_code in range(200, 300):
                    self.last_message = FakeMessagePayload(author=payload.sender, at=time.time())
                    self.log.debug("Message %s sent to discord bridge via webhook", message.event_id)
                    if self.config.get("webhook_wait") is True:
                        self.matrix_to_discord[message.event_id] = response.json()["id"]
                    return
                else:
                    self.log.warning(
                        "Failed to bridge message %s using webhook (%d). will fall back to websocket.",
                        message.event_id,
                        response.status_code,
                    )
            self.log.debug("Sending fallback message.")
            response = await client.post(self.jimmy_api, json=payload.model_dump())
            if response.status_code == 400:
                self.log.warning("Message %s was too long to send to discord.", message.event_id)
                data = response.json()
                if data["detail"] == "Message too long.":
                    await self.bot.add_reaction(room, message, "\N{PRINTER}\N{VARIATION SELECTOR-16}")
            elif response.status_code != 201:
                self.log.error(
                    "Error while sending message (%s) to discord bridge (%d): %s",
                    message.event_id,
                    response.status_code,
                    response.text,
                )
                await self.bot.add_reaction(room, message, "\N{CROSS MARK}")
                return
            else:
                self.log.debug("Message %s sent to discord bridge", message.event_id)

    @niobot.command("bind")
    async def bind(self, ctx: niobot.Context):
        """(discord bridge) Binds your discord account to your matrix account."""
        existing = await self.get_bound_account(ctx.message.sender)
        if existing:
            return await ctx.respond(
                "\N{cross mark} You have already bound your account to `{}`.\n"
                "Use `{}unbind` to unbind your account.".format(existing, self.bot.command_prefix),
            )
        async with httpx.AsyncClient() as client:
            response = await client.get(self.jimmy_api + "/bind/new", params={"mx_id": ctx.message.sender[1:]})
            if response.status_code == 200:
                data = response.json()
                if data["status"] != "pending":
                    return await ctx.respond("\N{cross mark} Failed to bind your account. Please try again later.")
                url = data["url"]
                await self.bot.send_message(
                    ctx.message.sender, "Please click [here]({}) to bind your discord account.".format(url)
                )
                await ctx.respond("\u23F3 I have sent you a link in a direct room.")
            else:
                self.log.warning(
                    "Unexpected status code %d while binding account: %s", response.status_code, response.text
                )
                await ctx.respond("\N{cross mark} Failed to bind your account. Please try again later.")

    @niobot.command("unbind")
    async def unbind(self, ctx: niobot.Context):
        """(discord bridge) Unbinds your account."""
        existing = await self.get_bound_account(ctx.message.sender)
        if not existing:
            return await ctx.respond("\N{cross mark} You have not bound your account to any discord account.")
        async with httpx.AsyncClient() as client:
            response = await client.delete(self.jimmy_api + "/bind/" + ctx.message.sender[1:])
            data = response.json()
            match data.get("status"):
                case "pending":
                    url = data["url"]
                    await self.bot.send_message(
                        ctx.message.sender, "Please click [here]({}) to unbind your discord account.".format(url)
                    )
                    await ctx.respond("\u23F3 I have sent you a link in a direct room.")
                case "ok":
                    await ctx.respond("\N{white heavy check mark} Your account has been unbound.")
                case _:
                    await ctx.respond("\N{cross mark} Failed to unbind your account. Please try again later.")
