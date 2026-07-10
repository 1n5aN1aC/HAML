"""Server configuration: JSON file over defaults, nothing fancier."""
import json
from pathlib import Path

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8080,
    "data_dir": "data",
    "admin_password": "haml",
}


def load_config(path=None):
    """Return config dict. `path` is an optional JSON file overriding DEFAULTS."""
    cfg = dict(DEFAULTS)
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    data_dir = Path(cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = Path(__file__).resolve().parent / data_dir
    cfg["data_dir"] = data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
