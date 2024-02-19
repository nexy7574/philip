import asyncio
import io
import json
import logging
import subprocess
import tempfile
import typing
from pathlib import Path
from urllib.parse import urlparse

import PIL.features
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
from typing import Optional, Union


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
        self.guild_id = self.config.get("guild_id", None)
        self.channel_id = self.config.get("channel_id", "!WrLNqENUnEZvLJiHsu:nexy7574.co.uk")
        self.webhook_url = self.config.get("webhook_url", None)
        if not self.webhook_url:
            self.log.warning("No webhook URL set for bridge - the other end will be ugly.")

        self.websocket_endpoint = self.config.get("websocket_endpoint")
        if not self.websocket_endpoint:
            self.websocket_endpoint = "wss://droplet.nexy7574.co.uk/jimmy/bridge/recv"
        self.jimmy_api_domain = urlparse(self.websocket_endpoint).hostname
        scheme = "https" if urlparse(self.websocket_endpoint).scheme == "wss" else "http"
        self.jimmy_api = scheme"://" + self.jimmy_api_domain + "/jimmy"

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
        self.bridge_lock = asyncio.Lock()

        self.bind_cache = {}
        # For the webhook.
        # {
        #     user_id: {"username": "raaa", "avatar": "https://cdn.discordapp.com/...", "expires": 123.45}
        # }

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
            url = "https://discord.com/api/v10/guilds/%d/members/%d" % (self.guild_id, user_id)
        else:
            url = "https://discord.com/api/v10/users/%d" % user_id
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": "Bot " + self.token
                }
            )
            if response.status_code == 200:
                data = response.json()
                user_data = data.get("user", data)
                display_name = data.get("nick", user_data["username"])
                avatar = None
                if data.get("avatar"):
                    avatar = AVATAR_URL % (user_id, data["avatar"])
                return {
                    "username": display_name,
                    "avatar": avatar,
                    "expires": time.time() + 86400
                }
    
    async def get_bound_account(self, sender: str) -> Optional[int]:
        """
        Fetches the user's bound discord account ID from the Jimmy v1 API.

        :param sender: The matrix sender account
        :return: The discord ID, if available.
        """
        if sender.startswith("@"):
            sender = sender[1:]
        
        async with httpx.AsyncClient() as client:
            response = await client.get(self.jimmy_api + "/bridge/bind/" + sender)
            if response.status_code == 200:
                return response.json()["discord"]

    async def get_image_from_cache(
            self,
            http: str,
            *,
            make_round: bool = False,
            encrypted: bool = False
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
                        if make_round:
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
                    if meta.get("name") == "twitter:image":
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

    def convert_image(self, path: Path, quality: int = 90, speed: int = 0) -> Path:
        speed = 6 - speed
        with tempfile.NamedTemporaryFile("wb", suffix=path.with_suffix(".webp").name, delete=False) as temp_fd:
            img = PIL.Image.open(path)
            kwargs = {
                "format": "webp",
                "quality": quality,
                "method": speed
            }
            if getattr(img, "is_animated", False) and PIL.features.check("webp_anim"):
                kwargs["save_all"] = True
            self.log.info("Converting image %r to webp with kwargs %r", path, kwargs)
            img.save(temp_fd, **kwargs)
            temp_fd.flush()
            return Path(temp_fd.name)

    async def poll_loop(self, client: httpx.AsyncClient):
        if not self.bot.is_ready.is_set():
            await self.bot.is_ready.wait()
        room = self.bot.rooms.get(self.channel_id)
        if not room:
            self.log.warning("No room for bridge!")
            return True

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

                    if payload.content:
                        new_content = ""
                        if self.should_prepend_username(payload):
                            avatar = await self.get_image_from_cache(payload.avatar, make_round=True)
                            if avatar:
                                avatar = '<img src="%s" width="16px" height="16px" alt="[\U0001f464]"> ' % avatar
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
                                        # Convert it to webp.
                                        if attachment.content_type != "image/gif":
                                            new_file = await niobot.run_blocking(
                                                self.convert_image,
                                                temp_file,
                                                speed=0,
                                                quality=80
                                            )
                                        else:
                                            new_file = temp_file
                                        file_attachment = await discovered.from_file(
                                            new_file,
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
        
        bound_account = await self.get_bound_account(message.sender)
        if bound_account:
            user_data = await self.get_discord_user(bound_account)
            if user_data:
                payload.sender = user_data["username"]
                if user_data["avatar"]:
                    payload.avatar = user_data["avatar"]

        async with httpx.AsyncClient() as client:
            if self.webhook_url:
                avatar = payload.avatar
                if not avatar:
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
                    self.log.debug("Message %s sent to discord bridge via webhook", message.event_id)
                    return
                else:
                    self.log.warning(
                        "Failed to bridge message %s using webhook (%d). will fall back to websocket.",
                        message.event_id,
                        response.status_code
                    )
            response = await client.post(
                self.jimmy_api + "/bridge",
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
            response = await client.get(
                self.jimmy_api + "/bridge/bind/new",
                params={"mx_id": ctx.message.sender[1:]}
            )
            if response.status_code == 200:
                data = await response.json()
                if data["status"] != "pending":
                    return await ctx.respond(
                        "\N{cross mark} Failed to bind your account. Please try again later."
                    )
                url = data["url"]
                await self.bot.send_message(
                    ctx.message.sender,
                    "Please click [here]({}) to bind your discord account.".format(url)
                )
                await ctx.respond(
                    "\u23F3 I have sent you a link in a direct room."
                )
    
    @niobot.command("unbind")
    async def unbind(self, ctx: niobot.Context):
        """(discord bridge) Unbinds your account."""
        existing = await self.get_bound_account(ctx.message.sender)
        if not existing:
            return await ctx.respond(
                "\N{cross mark} You have not bound your account to any discord account."
            )
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                self.jimmy_api + "/bridge/bind/" + ctx.message.sender[1:]
            )
            data = response.json()
            match data.get("status"):
                case "pending":
                    url = data["url"]
                    await self.bot.send_message(
                        ctx.message.sender,
                        "Please click [here]({}) to unbind your discord account.".format(url)
                    )
                    await ctx.respond(
                        "\u23F3 I have sent you a link in a direct room."
                    )
                case "ok":
                    await ctx.respond(
                        "\N{white heavy check mark} Your account has been unbound."
                    )
                case _:
                    await ctx.respond(
                        "\N{cross mark} Failed to unbind your account. Please try again later."
                    )
