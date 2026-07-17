"""Server configuration: JSON file over defaults, nothing fancier."""
import json
from pathlib import Path

DEFAULTS = {
    "host": "0.0.0.0",        # Listen on all interfaces
    "port": 80,               # Port for HAML REST API & WebSocket
    "data_dir": "data",       # Contains template & event state. (relative to server/)
    "admin_password": "haml", # Password for the admin REST endpoints
    "fcc_db_path": "datasets/fcc_amateur.sqlite", # Path to the local FCC ULS sqlite
}


def _resolve_relative_to_server(path):
    """Make a server-relative path absolute against this file's dir.

    Used for both `data_dir` and `fcc_db_path` so a config value of
    "data" or "datasets/foo" lands under the server install rather
    than the cwd the server was launched from. Unlike data_dir,
    fcc_db_path has no matching mkdir — the dataset is gitignored
    and a missing file is allowed (the server warns and runs).
    """
    p = Path(path)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parent / p


def load_config(path=None):
    """Return config dict. `path` is an optional JSON file overriding DEFAULTS."""
    cfg = dict(DEFAULTS)
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    data_dir = _resolve_relative_to_server(cfg["data_dir"])
    cfg["data_dir"] = data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    # Resolve fcc_db_path the same way (without mkdir — see helper docstring).
    cfg["fcc_db_path"] = _resolve_relative_to_server(cfg["fcc_db_path"])
    return cfg
