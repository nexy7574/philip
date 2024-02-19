import niobot
import httpx
import tempfile
import re


class FunModule(niobot.Module):

    @niobot.command()
    async def ping(self, ctx: niobot.Context):
        """Pings the bot."""
        latency = ctx.latency
        await ctx.respond(f"Pong! Approximately {latency:.2f}ms")

    @niobot.command()
    async def xkcd(self, ctx: niobot.Context, comic_number: int = None):
        """Fetches an XKCD comic.

        If none is provided, a random one is chosen."""
        async with httpx.AsyncClient() as session:
            if comic_number is None:
                response = await session.get("https://c.xkcd.com/random/comic/", follow_redirects=False)
                if response.status_code != 302:
                    await ctx.respond("Unable to fetch a random comic (HTTP %d)" % response.status_code)
                    return
                comic_number = re.match(r"http(s)?://xkcd.com/(\d+)/", response.headers["Location"]).group(2)
                comic_number = int(comic_number)

            response = await session.get("https://xkcd.com/%d/info.0.json" % comic_number)
            if response.status_code != 200:
                await ctx.respond("Unable to fetch comic %d (HTTP %d)" % (comic_number, response.status_code))
                return

            data = response.json()
            download = await session.get(data["img"])
            if download.status_code != 200:
                await ctx.respond("Unable to download comic %d (HTTP %d)" % (comic_number, download.status_code))
                return

            with tempfile.NamedTemporaryFile("wb", prefix="xkcd-comic-", suffix=".png") as file:
                file.write(download.content)
                file.flush()
                file.seek(0)
                attachment = await niobot.ImageAttachment.from_file(file.name)
                await ctx.respond(data["alt"], file=attachment)

    @niobot.command()
    async def avatar(self, ctx: niobot.Context, target: str = None, server: str = None):
        """Fetches and provides a download link to an avatar.

        Target can be 'self', for yourself, 'room' for the current room, or a user ID, or a room ID.

        Server is an optional server to download via. By default, this is your homeserver.
        """
        parsed: niobot.MatrixUser | niobot.MatrixRoom | str
        target = target or "self"
        target_lower = target.casefold()
        if target_lower not in ("self", "room"):
            try:
                parsed: niobot.MatrixRoom = await niobot.RoomParser.parse(ctx, None, target)
            except niobot.CommandParserError:
                try:
                    parsed: niobot.MatrixUser = niobot.parsers.MatrixUserParser.parse(ctx, None, target)
                except niobot.CommandParserError:
                    await ctx.respond("Unknown room or user ID.")
                    return
        else:
            if target_lower == "self":
                parsed = ctx.message.sender
            else:
                parsed = ctx.room

        if isinstance(parsed, niobot.MatrixUser):
            target_id = parsed.user_id
        elif isinstance(parsed, niobot.MatrixRoom):
            target_id = parsed.room_id
        else:
            target_id = parsed

        if not target_id.startswith(("!", "#")):
            # User
            response = await self.bot.get_profile(target_id)
            if not isinstance(response, niobot.ProfileGetResponse):
                await ctx.respond(f"Unable to fetch profile: `{response!r}`")
                return
            if not response.avatar_url:
                await ctx.respond("No avatar set")
                return
            url = response.avatar_url
        else:
            # Room
            if target_id not in self.bot.rooms:
                await ctx.respond("I'm not in that room. You can invite me, if you would like, and I will join.")
                return
            room = self.bot.rooms[target_id]
            url = room.room_avatar_url
            if not url:
                await ctx.respond("No avatar set")
                return

        homeserver = server
        if homeserver is None:
            _, homeserver = ctx.message.sender.rsplit(":", 1)
        http = await self.bot.mxc_to_http(url, homeserver)
        if not http.startswith("http"):
            http = "http://" + http
        return await ctx.respond(
            'Avatar for {target}: [`{mxc}`]({http})\n\n'
            '<img src="{mxc}" width="128px" height="128px" alt="avatar"/>'.format(
                target=target_id,
                mxc=url,
                http=http
            )
        )
