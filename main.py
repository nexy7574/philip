import nio
import niobot
import logging
from util import config

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
log = logging.getLogger("philip.runtime")
bot = niobot.NioBot(
    PHILIP_CONF.get("homeserver", "https://matrix.nexy7574.co.uk"),
    PHILIP_CONF.get("user_id", "@philip:nexy7574.co.uk"),
    PHILIP_CONF.get("device_id", "dev"),
    PHILIP_CONF.get("store_path", "./keystore"),
    command_prefix=PHILIP_CONF.get("command_prefix", "!"),
    owner_id=PHILIP_CONF.get("owner_id", "@nex:nexy7574.co.uk")
)
log.info("Philip starting.")
try:
    bot.mount_module("modules.discord_bridge")
except AssertionError as e:
    log.warning("Failed to load discord bridge: %s", e, exc_info=True)
bot.mount_module("modules.fun")
bot.mount_module("modules.user_eval")
bot.mount_module("modules.pypi_releases")
bot.mount_module("modules.ytdl")


@bot.on_event("ready")
async def on_ready(_):
    log.info("Logged in!")
    response = await bot.join("!8ybQEAbfsaJNnFWa:nexy7574.co.uk")
    if isinstance(response, nio.JoinError):
        log.critical(response.message)
        log.critical(response.status_code)
        log.critical(response.retry_after_ms)
        log.critical(response.soft_logout)


@bot.on_event("command_error")
async def on_command_error(ctx: niobot.Context, error: Exception):
    log.error("Error in command %r: %s", ctx.command, error, exc_info=True)
    await ctx.respond("Error: %s" % error)


if PHILIP_CONF.get("password"):
    bot.run(password=PHILIP_CONF["password"])
else:
    bot.run(access_token=PHILIP_CONF["access_token"])
