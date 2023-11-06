import niobot
import logging
from util import config

PHILIP_CONF = config["philip"]
if "logging" in config:
    LOGGING_CONF = config["logging"]
    log_level = LOGGING_CONF.get("level", "INFO").upper()
    assert log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), "Invalid log level"
    log_file = LOGGING_CONF.get("file", None)
    log_format = LOGGING_CONF.get("format", "%(asctime)s %(levelname)s %(name)s %(message)s")
    log_date_format = LOGGING_CONF.get("date_format", "%Y-%m-%d %H:%M:%S")
    if log_file:
        log_mode = LOGGING_CONF.get("file_mode", "w")
        assert log_mode in ("w", "a"), "Invalid log file mode. Must be (w)rite or (a)ppend"
        mirror_to_stdout = LOGGING_CONF.getboolean("mirror_to_stdout", False)
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


log = logging.getLogger("philip")
bot = niobot.NioBot(
    PHILIP_CONF.get("homeserver", "https://matrix.nexy7574.co.uk"),
    PHILIP_CONF.get("user_id", "@philip:nexy7574.co.uk"),
    PHILIP_CONF.get("device_id", "dev"),
    PHILIP_CONF.get("store_path", "./keystore"),
    command_prefix=PHILIP_CONF.get("command_prefix", "!"),
    owner_id=PHILIP_CONF.get("owner_id", "@nex:nexy7574.co.uk")
)
log.info("Philip starting.")


@bot.on_event("ready")
async def on_ready():
    log.info("Logged in!")
