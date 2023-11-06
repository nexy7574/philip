from pathlib import Path
import toml

CONFIG_PATH = Path(__file__).parent / "config.ini"
config = toml.load(CONFIG_PATH)
