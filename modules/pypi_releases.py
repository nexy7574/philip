import asyncio
import datetime
import logging

import niobot
import httpx
from xml.etree import ElementTree
from util import config


class PyPiReleases(niobot.Module):
    def __init__(self, bot: niobot.NioBot):
        super().__init__(bot)
        self.started_at = datetime.datetime.utcnow()
        self.log = logging.getLogger("philip.pypi_rss")

        self.config = config["philip"].get("pypi", {})
        self.url = self.config.get("url", "https://pypi.org/rss/project/nio-bot/releases.xml")
        self.room_id = self.config["room_id"]
        self.last_etag = None
        self.task = None

    @niobot.event("ready")
    async def on_event_loop_ready(self, _):
        self.task = asyncio.create_task(self.read_rss())

    async def rss_loop(self):
        self.log.info("Starting RSS Loop")
        while True:
            await self.read_rss()
            await asyncio.sleep(1800)

    async def read_rss(self):
        async with httpx.AsyncClient() as client:
            headers = {
                "User-Agent": niobot.__user_agent__
            }
            if self.last_etag:
                headers["If-None-Match"] = self.last_etag
            self.log.debug("Requesting RSS @ %s", self.url)
            response = await client.get(self.url, headers=headers)
            self.log.debug("Response: %r", response)
            if response.status_code == 304:
                return
            response.raise_for_status()
            self.last_etag = response.headers.get("etag", self.last_etag)
            self.log.debug("Set etag to %r", self.last_etag)
            root = ElementTree.fromstring(response.content)
            for item in root.findall("./channel/item"):
                self.log.debug("Found item in ./channel/item: %r", item)
                title = item.find("title").text
                self.log.debug("Title: %r", title)
                link = item.find("link").text
                self.log.debug("Link: %s", link)
                pub_date = item.find("pubDate").text
                parsed = datetime.datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z")
                self.log.debug("Date: %s (%r)", pub_date, parsed)
                if parsed < self.started_at:
                    self.log.debug("Entry is old(er than %s)", self.started_at.isoformat())
                    continue
                self.log.debug("Entry is new(er than %s), sending message", self.started_at.isoformat())

                await self.bot.send_message(
                    self.room_id,
                    f"[New release on PyPi! {title}]({link})",
                )
