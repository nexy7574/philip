import json
import logging
import pathlib
import asyncio
from urllib.parse import urlparse

import aiohttp
import niobot

import nio
import aiofiles
import magic
from util import config
from yt_dlp import YoutubeDL
import tempfile
import typing

YTDL_ARGS: typing.Dict[str, typing.Any] = {
    "outtmpl": "%(title).50s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "no_warnings": True,
    "quiet": True,
    'noprogress': True,
    "nooverwrites": True,
    'format': "(bv+ba/b)[filesize<=90M]/b",
    "format_sort": [
        "codec",
        "ext"
    ]
}


class YoutubeDownloadModule(niobot.Module):
    def __init__(self, *args):
        super().__init__(*args)
        self.log = logging.getLogger("philip.ytdl")
        self.lock = asyncio.Lock()
        self.config = config["philip"].get("ytdl", {})
        self.download_speed_megabits = self.config.get("download_speed_megabits", 900)
        self.upload_speed_megabits = self.config.get("upload_speed_megabits", 90)

    @property
    def download_speed_bits(self) -> float:
        """
        The expected download speed in bits.
        """
        return self.download_speed_megabits * (10**6)

    @property
    def upload_speed_bits(self) -> float:
        """
        The expected upload speed in bits.
        """
        return self.upload_speed_megabits * (10**6)

    @property
    def download_speed_bytes(self) -> float:
        """
        The expected download speed in bytes instead of bits.
        """
        return self.download_speed_bits / 8

    @property
    def upload_speed_bytes(self) -> float:
        """
        The expected upload speed in bytes instead of bits.
        """
        return self.upload_speed_bits / 8

    def _download(self, url: str, download_format: str, *, temp_dir: str) -> typing.List[pathlib.Path]:
        args = YTDL_ARGS.copy()
        dl_loc = pathlib.Path(temp_dir) / "dl"
        tmp_loc = pathlib.Path(temp_dir) / "tmp"
        dl_loc.mkdir(parents=True, exist_ok=True)
        tmp_loc.mkdir(parents=True, exist_ok=True)
        args["paths"] = {
            "temp": str(tmp_loc),
            "home": str(dl_loc),
        }
        if download_format:
            args["format"] = download_format
        else:
            args["format"] = "(bv+ba/b)[filesize<=90M]"
        args["format"] = "(%s)[vcodec!=h265]" % args["format"]

        with YoutubeDL(args) as ytdl_instance:
            self.log.info("Downloading %s with format: %r", url, args["format"])
            ytdl_instance.download(
                [url]
            )

        x = list(dl_loc.iterdir())
        return x

    async def upload_files(self, file: pathlib.Path):
        stat = file.stat()
        # max 99Mb
        if stat.st_size > 99 * 1024 * 1024:
            self.log.warning("File %s is too big (%.2f megabytes)", file, stat.st_size / 1024 / 1024)
            return
        mime = magic.Magic(mime=True).from_file(file)
        self.log.debug("File %s is %s", file, mime)
        metadata = await niobot.run_blocking(niobot.get_metadata, file) or {}
        if not metadata.get("streams"):
            self.log.warning("No streams for %s", file)
            return

        attachment = await niobot.which(file).from_file(file)
        self.log.debug("Uploading %r", file)
        await attachment.upload(self.client)
        self.log.debug("Uploaded %r -> %r.", file, attachment)
        return attachment

    async def get_video_info(self, url: str, secure: bool = True) -> dict:
        """Extracts JSON information about the video"""
        args = YTDL_ARGS.copy()
        with YoutubeDL(args) as ytdl_instance:
            # noinspection PyTypeChecker
            info = await asyncio.to_thread(
                ytdl_instance.extract_info,
                url,
                download=False
            )
            info = ytdl_instance.sanitize_info(info, remove_private_keys=secure)
        self.log.debug("ytdl info for %s: %r", url, info)
        return info

    @staticmethod
    def resolve_thumbnail(info: dict, resolution: str = None) -> typing.Optional[str]:
        """Resolves the thumbnail URL from the info dict"""
        width, height = 0, 0
        if resolution:
            width, height = map(int, resolution.split("x"))
        if info.get("thumbnail") and isinstance(info["thumbnail"], str):
            return info["thumbnail"]
        if info.get("thumbnails"):
            if isinstance(info["thumbnails"], list):
                thumbs = info["thumbnails"].copy()
                thumbs.sort(key=lambda x: x.get("preference", 0), reverse=True)
                if width and height:
                    def _val(x):
                        t_w = int(x.get("width", 800))
                        t_h = int(x.get("height", 600))
                        score_h = abs(t_h - height)
                        score_w = abs(t_w - width)
                        # lowest score first
                        return score_h + score_w
                    thumbs.sort(key=_val)
                return thumbs[0]["url"]

    @niobot.command(
        "ytdl",
        help="Downloads a video from YouTube", 
        aliases=['yt', 'dl', 'yl-dl', 'yt-dlp'], 
        usage="<url> [format]",
    )
    async def ytdl(self, ctx: niobot.Context, url: str, download_format: str = None):
        """Downloads a video from YouTube"""
        if ctx.room.encrypted:
            await ctx.respond("This command is not available in encrypted rooms.")
            return
        if self.lock.locked():
            msg = await ctx.respond("Waiting for previous download to finish...")
        else:
            msg = await ctx.respond("Resolving...")
        async with self.lock:
            room = ctx.room
            dl_format = download_format or "(bv+ba/b)[filesize<=90M]/b"
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    info = await self.get_video_info(url)
                    if not info:
                        await msg.edit("Could not get video info (Restricted?)")
                        return
                    size = int(info.get("filesize") or info.get("filesize_approx") or 30 * 1024 * 1024)
                    ETA = (size * 8) / self.download_speed_bits
                    minutes, seconds = divmod(ETA, 60)
                    seconds = round(seconds)

                    eta_text = "%d seconds" % seconds
                    if minutes:
                        eta_text = "%d minutes and %d seconds" % (minutes, seconds)

                    await msg.edit(
                        "Downloading [%r](%s) (ETA %s)..." % (
                            info["title"],
                            info["original_url"],
                            eta_text
                        )
                    )
                    self.log.info("Downloading %s to %s", url, temp_dir)
                    files = await niobot.run_blocking(self._download, url, dl_format, temp_dir=temp_dir)
                    self.log.info("Downloaded %d files", len(files))
                    if not files:
                        await msg.edit("No files downloaded.")
                        return
                    await msg.edit("Processing...")
                    sent = False
                    for file in files:
                        size_mb = file.stat().st_size / 1024 / 1024
                        upload_speed = getattr(config, "UPLOAD_SPEED_BITS", 15) / (10**6)
                        ETA = ((size_mb / 1024 / 1024) * 8) / upload_speed
                        minutes, seconds = divmod(ETA, 60)
                        seconds = round(seconds)
                        await msg.edit(
                            "Uploading %s (%dMb, ETA %s)..." % (
                                file.name,
                                size_mb,
                                "%d minutes and %d seconds" % (minutes, seconds) if minutes else "%d seconds" % seconds
                            )
                        )
                        self.log.info("Uploading %s (%dMb)", file.name, size_mb)
                        try:
                            attachment = await self.upload_files(file)
                            await self.client.send_message(
                                room,
                                content=file.name,
                                file=attachment,
                                reply_to=ctx.message
                            )
                        except Exception as e:
                            self.log.error("Error: %s", e, exc_info=e)
                            await msg.edit("Error: %r" % e)
                            return
                        sent = True

                    if sent:
                        await msg.edit("Completed, downloaded [your video]({})".format("url"))
                        await asyncio.sleep(10)
                        await msg.delete("Command completed.")
            except Exception as e:
                self.log.error("Error: %s", e, exc_info=e)
                await msg.edit("Error: " + str(e))
                return

    @niobot.command("ytdl-metadata", arguments=[niobot.Argument("url", str, description="The URL to download.")])
    async def ytdl_metadata(self, ctx: niobot.Context, url: str):
        """Downloads and exports a JSON file with the metadata for the given video."""
        msg = await ctx.respond("Downloading...")
        extracted = await self.get_video_info(url, secure=True)
        if not extracted:
            await msg.edit("Could not get video info (Restricted?)")
            return
        pretty = json.dumps(extracted, indent=4, default=repr)
        if len(pretty) < 2000:
            await msg.edit("```json\n%s\n```" % pretty)
            return

        with tempfile.NamedTemporaryFile(suffix=".json") as temp_file:
            p = pathlib.Path(temp_file.name)
            with open(temp_file.name, "w") as __temp_file:
                json.dump(extracted, __temp_file, indent=4, default=repr)
                __temp_file.flush()
            upload = niobot.FileAttachment(p, "application/json")
            await ctx.respond("info.json", file=upload)
            await msg.delete()

    @niobot.command("media-info")
    async def media_info(self, ctx: niobot.Context, event: niobot.Event):
        """Views information for an attached image/video/audio file."""
        if not isinstance(event, (niobot.RoomMessageMedia,)):
            await ctx.respond("Event is not an image, video, or audio file (%r)" % type(event))
            return

        msg = await ctx.respond("Downloading, please wait.")
        response = await self.bot.download(event.url)
        if not isinstance(response, niobot.DownloadResponse):
            await msg.edit("Could not download media: %r" % response)
            return
        suffix = pathlib.Path(response.filename or "no_file_name.bin").suffix
        with tempfile.NamedTemporaryFile("wb", suffix=suffix) as _file:
            _file.write(response.body)
            _file.flush()
            _file.seek(0)
            await msg.edit('Processing, please wait.')

            attachment = await niobot.which(_file.name).from_file(_file.name)
            metadata = await niobot.run_blocking(niobot.get_metadata, _file.name)

            duration = getattr(attachment, 'duration', 'N/A')
            resolution = "{0.width}x{0.height}".format(attachment) if hasattr(attachment, 'width') else 'N/A'

            lines = [
                '# Summary',
                '- **File Type**: %s' % attachment.mime_type,
                '- **File Size**: {:.1f} MiB ({:,} bytes)'.format(attachment.size_as('mib'), len(response.body)),
                '- **File Name**: `%s`' % (response.filename or pathlib.Path(_file.name).name),
                '- **URL**: HTTP: %s | MXC: %s' % (
                    await self.bot.mxc_to_http(event.url, ctx.message.sender.split(":", 1)[-1]),
                    event.url
                ),
                "",
                '# Metadata',
                '- **Duration**: %s seconds' % duration,
                '- **Resolution**: %s' % resolution,
                '- **MIME Type**: %s' % attachment.mime_type,
                '',
                '# Raw probe info',
                '```json\n%s\n```' % json.dumps(metadata, indent=4, default=repr)
            ]
            await msg.edit("\n".join(lines))
