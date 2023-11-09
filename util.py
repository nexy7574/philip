from pathlib import Path
import toml

CONFIG_PATH = Path(__file__).parent / "config.toml"
config = toml.load(CONFIG_PATH)
config.setdefault("philip", {})
