"""Event lifecycle: create from template, list, activate, delete, back up
(ADR-0002).

The data directory holds:
  state.json           -> {"active": "events/<file>.db"} (which Event is live)
  events/*.db          -> one SQLite file per Event
  backups/*.db         -> timestamped copies made by the backup action
"""
import json
import re
import sqlite3
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

import db


def _state_path(data_dir):
    return Path(data_dir) / "state.json"


def get_active_path(data_dir):
    """Path of the active Event database, or None. Raises ValueError when
    state.json is corrupt — the operator must fix or delete the file."""
    state_file = _state_path(data_dir)
    if not state_file.exists():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{state_file} is corrupt: {exc} — fix or delete it") from None
    active = state.get("active") if isinstance(state, dict) else None
    if not isinstance(state, dict) or not (active is None or isinstance(active, str)):
        raise ValueError(
            f"{state_file} is corrupt: expected a JSON object with a string"
            " 'active' — fix or delete it")
    if not active:
        return None
    path = Path(data_dir) / active
    return path if path.exists() else None


def _set_active(data_dir, db_path):
    relative = Path(db_path).relative_to(data_dir).as_posix()
    _state_path(data_dir).write_text(
        json.dumps({"active": relative}, indent=2), encoding="utf-8"
    )


def validate_location(location):
    """Validate an optional operating position, raising ValueError when bad.
    The client uses it as the reference point for the live distance readout
    next to the callsign box; None means no distances are shown."""
    if location is None:
        return None
    if not isinstance(location, dict) or set(location) != {"latitude", "longitude"}:
        raise ValueError(
            "'location' must be an object with exactly 'latitude' and 'longitude'")
    for key, bound in (("latitude", 90), ("longitude", 180)):
        value = location[key]
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not -bound <= value <= bound):
            raise ValueError(
                f"location '{key}' must be a number between -{bound} and {bound}")
    return location


def create_event(data_dir, template, name, station_callsign, location=None,
                 local_exchange=None):
    """Create a new Event database from a validated template and make it active.
    Returns the new Event's meta dict."""
    event_uuid = str(uuidlib.uuid4())
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "event"
    events_dir = Path(data_dir) / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / f"{slug}-{event_uuid[:8]}.db"

    config = {
        "fields": template["fields"],
        "bands": template["bands"],
        "modes": template["modes"],
        "duplicate_type": template["duplicate_type"],
        "location": location,
        "export": template.get("export"),
    }
    conn = db.open_db(path)
    db.meta_set(conn, "event_uuid", event_uuid)
    db.meta_set(conn, "event_name", name)
    db.meta_set(conn, "station_callsign", station_callsign)
    if local_exchange:
        db.meta_set(conn, "local_exchange", local_exchange)
    db.meta_set(conn, "template_name", template["name"])
    db.meta_set(conn, "created_at", db.now_iso())
    db.meta_set(conn, "config", json.dumps(config))
    conn.close()

    _set_active(data_dir, path)
    return event_meta(path)


# meta dict key -> meta table key
EVENT_META_KEYS = {
    "event_uuid": "event_uuid",
    "name": "event_name",
    "station_callsign": "station_callsign",
    "local_exchange": "local_exchange",
    "template_name": "template_name",
    "created_at": "created_at",
}


def event_meta(db_path):
    """Read identifying meta from an Event database file. A file that isn't
    a readable Event database yields all-None meta (callers skip on the
    missing event_uuid) rather than an error."""
    try:
        conn = db.open_db_readonly(db_path)
        try:
            return {key: db.meta_get(conn, meta_key)
                    for key, meta_key in EVENT_META_KEYS.items()}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"warning: skipping unreadable event db {db_path}: {exc}")
        return {key: None for key in EVENT_META_KEYS}


def list_events(data_dir):
    """[{...meta, active: bool}] for every Event database on disk."""
    active = get_active_path(data_dir)
    result = []
    for path in sorted((Path(data_dir) / "events").glob("*.db")):
        meta = event_meta(path)
        if meta["event_uuid"]:
            meta["active"] = path == active
            result.append(meta)
    return result


def find_event_path(data_dir, event_uuid):
    """Locate an Event database file by its Event UUID, or None."""
    for path in (Path(data_dir) / "events").glob("*.db"):
        if event_meta(path)["event_uuid"] == event_uuid:
            return path
    return None


def activate_event(data_dir, event_uuid):
    """Mark an existing Event as active. Returns its path, or None if unknown."""
    path = find_event_path(data_dir, event_uuid)
    if path:
        _set_active(data_dir, path)
    return path


def delete_event(data_dir, event_uuid):
    """Remove an Event database file. Returns True when deleted, False when
    unknown; raises ValueError for the active Event (its file is held open
    and connected clients are logging into it)."""
    path = find_event_path(data_dir, event_uuid)
    if path is None:
        return False
    if path == get_active_path(data_dir):
        raise ValueError("cannot delete the active event")
    path.unlink()
    return True


def backup_event(conn, data_dir, event_name):
    """Copy the active Event database (via the SQLite backup API, safe while
    open) into backups/. Returns the backup file path."""
    backups_dir = Path(data_dir) / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (event_name or "event").lower()).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = backups_dir / f"{slug}-{stamp}.db"
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return path
