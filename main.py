from threading import Thread, Event
import niobot
import random
import time
import httpx
import logging
from util import config


class KillableThread(Thread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kill = Event()


class KumaThread(KillableThread):
    def __init__(self, url: str, interval: float = 60.0):
        super().__init__(target=self.run)
        self.daemon = True
        self.log = logging.getLogger("philip.status")
        self.url = url
        self.interval = interval
        self.kill = Event()
        self.retries = 0
    
    def calculate_backoff(self) -> float:
        t = (2 * 2 ** self.retries) + random.uniform(0, 1)
        # T can never exceed self.interval
        return min(0, max(self.interval, t))
    
    def run(self) -> None:
        with httpx.Client(http2=True) as client:
            while not self.kill.is_set():
                start_time = time.time()
                try:
                    self.retries += 1
                    response = client.get(self.url)
                except httpx.HTTPError as error:
                    self.log.error("Failed to connect to uptime-kuma: %r: %r", self.url, error, exc_info=error)
                    time.sleep(self.calculate_backoff())
                    continue
                if response.status_code != 200:
                    self.log.error("Failed to ping %r: %r; %r", response.url, response, response.text or '-')
                self.retries = 0
                end_time = time.time()
                timeout = self.interval - (end_time - start_time)
                self.kill.wait(timeout)


log = logging.getLogger("philip.runtime")
PHILIP_CONF = config["philip"]
if "logging" in PHILIP_CONF:
    LOGGING_CONF = PHILIP_CONF["logging"]
    log_level = LOGGING_CONF.get("level", "INFO").upper()
    assert log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), "Invalid log level"
    log_file = LOGGING_CONF.get("file", None)
    log_format = LOGGING_CONF.get("format", "%(asctime)s %(levelname)s %(name)s %(message)s")
    log_date_format = LOGGING_CONF.get("date_format", "%Y-%m-%d %H:%M:%S")
    if log_file:
        log_mode = LOGGING_CONF.get("file_mode", "w")
        assert log_mode in ("w", "a"), "Invalid log file mode. Must be (w)rite or (a)ppend"
        mirror_to_stdout = LOGGING_CONF.get("mirror_to_stdout", False)
    else:
        log_mode = None
        mirror_to_stdout = False

    logging.basicConfig(
        filename=log_file,
        filemode=log_mode,
        level=log_level,
        format=log_format,
        datefmt=log_date_format
    )
    if mirror_to_stdout:
        console = logging.StreamHandler()
        console.setLevel(log_level)
        formatter = logging.Formatter(log_format, log_date_format)
        console.setFormatter(formatter)
        logging.getLogger("").addHandler(console)

    if "silence" in LOGGING_CONF and isinstance(LOGGING_CONF["silence"], list):
        for namespace in LOGGING_CONF["silence"]:
            logging.getLogger(namespace).setLevel(logging.ERROR)
            logging.info("Silenced logger %r (set to ERROR)", namespace)


logging.getLogger("peewee").setLevel(logging.CRITICAL)
logging.getLogger("nio.responses").setLevel(logging.ERROR)
bot = niobot.NioBot(
    PHILIP_CONF.get("homeserver", "https://matrix.nexy7574.co.uk"),
    PHILIP_CONF.get("user_id", "@philip:nexy7574.co.uk"),
    PHILIP_CONF.get("device_id", "dev"),
    PHILIP_CONF.get("store_path", "./keystore"),
    command_prefix=PHILIP_CONF.get("command_prefix", "!"),
    owner_id=PHILIP_CONF.get("owner_id", "@nex:nexy7574.co.uk")
)
log.info("Philip starting.")
if PHILIP_CONF.get("uptime_kuma_url"):
    t = KumaThread(PHILIP_CONF["uptime_kuma_url"], PHILIP_CONF.get("uptime_kuma_interval", 60.0))
    log.info("Started UptimeKuma thread")
else:
    t = KillableThread(target=lambda: None)
    log.info("Started dummy status thread")
t.start()
log.info("Loading modules")
modules = [
    ["modules.discord_bridge", False],
    ["modules.fun", True],
    ["modules.user_eval", True],
    ["modules.pypi_releases", True],
    ["modules.ytdl", True],
    ["modules.support", True]
]
for module_location, mandatory in modules:
    try:
        perf_start = time.perf_counter()
        bot.mount_module(module_location)
        perf_end = time.perf_counter()
    except (Exception, AssertionError) as e:
        if mandatory:
            log.critical("Failed to load %r: %s", module_location, e, exc_info=True)
            raise
        else:
            log.warning("Failed to load module %r: %s", module_location, e, exc_info=True)
    else:
        log.info("Loaded %r in %.2fms successfully.", module_location, (perf_end - perf_start) * 1000)


@bot.on_event("ready")
async def on_ready(_):
    log.info("Logged in!")


@bot.on_event("command_error")
async def on_command_error(ctx: niobot.Context, error: Exception):
    if isinstance(error, niobot.NioBotException):
        error = error.bottom_of_chain()
    log.error("Error in command %r: %s", ctx.command, error, exc_info=error)
    await ctx.respond("Error: %s" % error)


@bot.command()
async def leave(ctx: niobot.Context, room: niobot.MatrixRoom = None):
    """Leaves the room.

    If room is not specified, leaves the current room.
    You cannot specify room unless you're the bot owner.

    You must have the KICK_MEMBERS permission to use this command.
    """
    if room is not None:
        if ctx.message.sender != ctx.client.owner_id:
            await ctx.respond("You must be the bot owner to leave a room you're not currently in.")
            return
    else:
        room: niobot.MatrixRoom = ctx.room

    if not room.power_levels.can_user_kick(ctx.message.sender) and not ctx.client.is_owner(ctx.message.sender):
        await ctx.respond("You must have the KICK power level to use this command.")
        return

    response = await ctx.client.room_leave(room.room_id)
    if isinstance(response, niobot.RoomLeaveError):
        return await ctx.respond(f"\N{cross mark} Failed to leave {room.room_id} - `{response!r}`")
    if room.room_id != ctx.room.room_id:
        return await ctx.client.add_reaction(ctx.room, ctx.message, "\N{white heavy check mark}")


if PHILIP_CONF.get("password"):
    bot.run(password=PHILIP_CONF["password"])
else:
    bot.run(access_token=PHILIP_CONF["access_token"])
