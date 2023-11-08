import niobot
import httpx
import tempfile
import re


class FunModule(niobot.Module):
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