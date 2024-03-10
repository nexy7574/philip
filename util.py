from pathlib import Path
import toml
from importlib.metadata import version
import httpx
import typing
from typing import Literal

if typing.TYPE_CHECKING:
    from modules.discord_bridge import BridgePayload

CONFIG_PATH = Path(__file__).parent / "config.toml"
config = toml.load(CONFIG_PATH)
config.setdefault("philip", {})
USER_AGENT = "Philip/1.0 (httpx/{}; nio-bot/{}, +https://github.com/nexy7574/philip)".format(
    version("httpx"), version("nio-bot")
)
USER_AGENT_MOZILLA = "Mozilla/5.0 (%s)" % USER_AGENT


class JimmyAPI:
    def __init__(self, _config: dict = None):
        if _config is None:
            _config = config["philip"].get("bridge", {})

        self.config = _config
        self.websocket_endpoint = self.config.get("websocket_endpoint")
        self.http_base = self.config.get("bridge_endpoint", "https://nexy7574.co.uk/jimmy/v1")
        if self.http_base.endswith("/bridge"):
            self.http_base = self.http_base[:-7]
        self.token = self.config.get("token")

        self.guild_id = self.config.get("guild_id")
        self.default_channel_id = self.config.get("channel_id")

    def session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.http_base,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": USER_AGENT,
            },
            timeout=30,
        )

    async def ping(self) -> dict[str, str | float | bool]:
        """
        Ping the Jimmy API
        """
        async with self.session() as client:
            response = await client.get("/ping")
            response.raise_for_status()
            return response.json()

    async def new_bridge_bind(self, user_id: str) -> dict[str, str]:
        """
        Creates a new bridge bind, returning the authentication URL.
        """
        async with self.session() as client:
            if user_id.startswith("@"):
                user_id = user_id[1:]
            response = await client.get("/bridge/bind", query={"mx_id": user_id})
            response.raise_for_status()
            return response.json()

    async def get_bridge_bind(self, user_id: str) -> dict[str, str] | None:
        """
        Gets an existing bridge bind for the given user, returning None if one does not exist.
        """
        async with self.session() as client:
            if user_id.startswith("@"):
                user_id = user_id[1:]
            response = await client.get("/bridge/bind/" + user_id)
            if response.status_code == 404:
                return
            response.raise_for_status()
            return response.json()

    async def delete_bridge_bind(self, user_id: str) -> None:
        """
        Deletes an existing bridge bind for the given user. Returns the URL to authenticate with.
        """
        async with self.session() as client:
            if user_id.startswith("@"):
                user_id = user_id[1:]
            response = await client.delete("/bridge/bind/" + user_id)
            response.raise_for_status()

    async def proxy_message(self, payload: "BridgePayload") -> dict[str, str | list[str]]:
        """Proxies a message via Jimmy where a webhook is unavailable."""
        async with self.session() as client:
            response = await client.post("/bridge", json=payload.dict())
            response.raise_for_status()
            return response.json()


class DiscordAPI:
    def __init__(self, version: int = 10):
        self.base_url = f"https://discord.com/api/v{version}"
        self.token = config["philip"].get("bridge", {}).get("token")

    def session(self, base_url: str | None = ..., include_token: bool = False) -> httpx.AsyncClient:
        if base_url is ...:
            base_url = self.base_url
        headers = {"User-Agent": USER_AGENT}
        if include_token:
            headers["Authorization"] = f"Bot {self.token}"
        kwargs = dict(headers=headers, timeout=30)
        if base_url:
            kwargs["base_url"] = base_url
        return httpx.AsyncClient(**kwargs)

    async def send_webhook(
        self,
        webhook_url: str,
        content: str,
        username: str | None = None,
        avatar_url: str | None = None,
        tts: bool | None = None,
        embeds: list[dict] | None = None,
        allowed_mentions: dict | None = None,
        *,
        wait: bool = False,
    ) -> dict:
        """
        Sends a message via a webhook, returning the resulting message if wait=True.
        """
        payload = {
            "content": content,
        }
        if username:
            payload["username"] = username
        if avatar_url:
            payload["avatar_url"] = avatar_url
        if tts is not None:
            payload["tts"] = tts
        if embeds:
            payload["embeds"] = embeds
        if allowed_mentions:
            payload["allowed_mentions"] = allowed_mentions

        async with self.session(base_url=None) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
            return response.json()

    async def get_webhook_message(self, webhook_url: str, message_id: int) -> dict | None:
        """Fetches a message sent by the webhook, returning None if not found."""
        async with self.session(base_url=None) as client:
            response = await client.get(f"{webhook_url}/messages/{message_id}")
            if response.status_code == 404:
                return
            response.raise_for_status()
            return response.json()

    async def edit_webhook_message(
        self,
        webhook_url: str,
        message_id: int,
        content: str,
        embeds: list[dict] | None = None,
        allowed_mentions: dict | None = None,
    ) -> dict:
        """Edits a message sent by the webhook."""
        payload = {"content": content}
        if embeds:
            payload["embeds"] = embeds

        async with self.session(base_url=None) as client:
            response = await client.patch(f"{webhook_url}/messages/{message_id}", json=payload)
            response.raise_for_status()
            return response.json()

    async def delete_webhook_message(self, webhook_url: str, message_id: int) -> None:
        """Deletes a message sent by the webhook."""
        async with self.session(base_url=None) as client:
            response = await client.delete(f"{webhook_url}/messages/{message_id}")
            response.raise_for_status()

    async def get_user(self, user_id: int) -> dict | None:
        """Fetches a user by their ID, returning None if not found."""
        async with self.session(include_token=True) as client:
            response = await client.get(f"/users/{user_id}")
            if response.status_code == 404:
                return
            response.raise_for_status()
            return response.json()

    async def get_member(self, guild_id: int, user_id: int) -> dict | None:
        """Fetches a member by their ID, returning None if not found."""
        async with self.session(include_token=True) as client:
            response = await client.get(f"/guilds/{guild_id}/members/{user_id}")
            if response.status_code == 404:
                return
            response.raise_for_status()
            return response.json()

    def get_avatar_url(
        self,
        user_id: int,
        avatar_hash: str,
        image_format: Literal["jpeg", "png", "webp", "gif"] = "webp",
        size: int = 1024,
    ) -> str:
        """Returns the avatar URL for the discord user with the given params"""
        if image_format == "gif" and not avatar_hash.startswith("a_"):
            image_format = "webp"

        return "https://cdn.discordapp.com/avatars/%d/%s.%s?size=%d" % (user_id, avatar_hash, image_format, size)
