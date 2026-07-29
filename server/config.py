"""Server configuration: JSON file over defaults, nothing fancier."""
import json
from pathlib import Path

# Default Config location when no argument is passed.
# In practice, it will never be passed, but the smoke tests require the command-line argument.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULTS = {
    "host": "0.0.0.0",                  # Listen interface
    "port": 80,                         # Port for HAML REST API & WebSocket
    "data_dir": "data",                 # Directory for template & event state. (relative to server/)
    "admin_password": "haml",           # Password for the admin REST endpoints
    "auto_backup_interval_minutes": 15, # Automatic backup cadence; 0 disables the loop
    "lookup_db_max_age_days": 30,       # Warning if sqlite is older than this
    "lookup_db_path": "datasets/lookup_data.sqlite",    # Path to the local lookup dataset sqlite
    "prefix_lst_path": "datasets/Prefix.lst",           # Path to the VE3NEA CallParser Prefix.lst
    "ultracheck_db_path": "datasets/ultracheck.sqlite", # Path to the partial-callsign search sqlite
}

def _resolve_relative_to_server(path):
    """Make a server-relative path absolute against this file's dir.

    Used for both `data_dir` and `lookup_db_path` so a config value of
    "data" or "datasets/foo" lands under the server install rather
    than the cwd the server was launched from. Unlike data_dir,
    lookup_db_path has no matching mkdir — the dataset is gitignored
    and a missing file is allowed (the server warns and runs).
    """
    p = Path(path)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parent / p

def load_config(path=None):
    """Return config dict. `path` is an optional JSON file overriding DEFAULTS.
    Fallas back to a built-in default location defined above for most usage."""
    cfg = dict(DEFAULTS)
    if path is None and DEFAULT_CONFIG_PATH.is_file():
        path = DEFAULT_CONFIG_PATH
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    data_dir = _resolve_relative_to_server(cfg["data_dir"])
    cfg["data_dir"] = data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    # Resolve all the datasets paths' the same way (without mkdir — see helper docstring).
    cfg["lookup_db_path"] = _resolve_relative_to_server(cfg["lookup_db_path"])
    cfg["prefix_lst_path"] = _resolve_relative_to_server(cfg["prefix_lst_path"])
    cfg["ultracheck_db_path"] = _resolve_relative_to_server(cfg["ultracheck_db_path"])
    return cfg