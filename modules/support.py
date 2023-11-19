import datetime
import logging
import textwrap

import httpx
import niobot
from bs4 import BeautifulSoup

from util import config


class SupportModule(niobot.Module):
    def __init__(self, bot: niobot.NioBot):
        super().__init__(bot)
        self.log = logging.getLogger("philip.modules.support")
        self.config = config["philip"].get("support", {})
        self.room_id = self.config.get("room_id")

    def is_in_support_room(self, ctx: niobot.Context):
        if self.room_id:
            return ctx.room.room_id == self.room_id
        return False

    @niobot.event("message")
    async def on_message(self, room: niobot.MatrixRoom, message: niobot.RoomMessageText):
        if message.body.strip() == self.bot.user_id:
            t = textwrap.dedent(
                """
                Hello {}! I am Philip. My prefix is `!`, and you can see a list of commands via `!help`.
                You can contact my developer, [@nex:nexy7574.co.uk](https://matrix.to/#/@nex:nexy7574.co.uk).
                *If you don't want me here, you can get an admin or mod to kick me. I won't be offended.*
                *Powered by [NioBot](https://nio-bot.dev)*
                """.format(message.sender)
            )
            return await self.bot.send_message(
                room.room_id,
                t,
                message_type="m.notice",
                reply_to=message.event_id
            )

    @niobot.command("python-eol")
    async def python_eol(self, ctx: niobot.Context, no_table: bool = False):
        """Get the EOL dates for Python versions

        If no_table is False, the output will be an HTML table.
        In the event your client does not support HTML tables, set no_table to True, and the output will be a list.
        """

        msg = await ctx.respond("Loading...")
        async with httpx.AsyncClient() as session:
            response = await session.get("https://devguide.python.org/versions/")
            if response.status_code != 200:
                return await msg.edit("Error: %s `%s`" % (response.status_code, response.text))

        soup = BeautifulSoup(response.text, "html.parser")
        supported_table = soup.find_all("section", id="supported-versions")[0].find_all("table")[0]
        unsupported_table = soup.find_all("section", id="unsupported-versions")[0].find_all("table")[0]
        if no_table is False:
            lines = [
                "# Supported Python Versions",
                supported_table.prettify(),
                "",
                "# EOL Versions",
                unsupported_table.prettify()
            ]
            return await msg.edit("\n".join(lines))

        else:
            table = {}
            for row in supported_table.find_all("tbody")[0].find_all("tr"):
                branch, schedule, status, first_release, eol_date, release_manager = row.find_all("td")
                table[branch.text] = {
                    "schedule": schedule.text,
                    "status": status.text,
                    "first_release": datetime.datetime.strptime(first_release.text, "%Y-%m-%d").date(),
                    "eol_date": datetime.datetime.strptime(eol_date.text, "%Y-%m").date(),
                    "release_manager": release_manager.text,
                    "is_eol": False
                }

            for row in unsupported_table.find_all("tbody")[0].find_all("tr"):
                branch, schedule, status, first_release, eol_date, release_manager = row.find_all("td")
                self.log.debug(
                    "Branch: %s, schedule: %s, status: %s, first_release: %s, eol_date: %s, release_manager: %s",
                    branch.text,
                    schedule.text,
                    status.text,
                    first_release.text,
                    eol_date.text,
                    release_manager.text
                )
                table[branch.text] = {
                    "schedule": schedule.text,
                    "status": status.text,
                    "first_release": datetime.datetime.strptime(first_release.text, "%Y-%m-%d").date(),
                    "eol_date": datetime.datetime.strptime(eol_date.text, "%Y-%m-%d").date(),
                    "release_manager": release_manager.text,
                    "is_eol": True
                }

            lines = []
            for version_name, data in table.items():
                self.log.debug(version_name, data)
                text = "[{}]({}) - [released]({}) {} ({:,} days ago), expires {}".format(
                    version_name,
                    f"https://peps.python.org/{data['schedule'].lower().replace(' ', '-')}/#schedule",
                    f"https://www.python.org/downloads/release/python-{version_name.strip('.')}/",
                    data["first_release"].strftime("%Y-%m-%d"),
                    (datetime.datetime.utcnow().date() - data["first_release"]).days,
                    data["eol_date"].strftime("%Y-%m"),
                )

                if data["eol_date"] > datetime.datetime.utcnow().date():
                    eol_in = (data["eol_date"] - datetime.datetime.utcnow().date()).days
                    text += f" (in {eol_in:,} days)"
                else:
                    eol_ago = (datetime.datetime.utcnow().date() - data["eol_date"]).days
                    text += f" ({eol_ago:,} days ago)"

                if data["is_eol"]:
                    text = f"<del>{text}</del>"
                lines.append("* " + text)

            return await msg.edit("\n".join(lines))
