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
    """Path of the active Event database, or None."""
    state_file = _state_path(data_dir)
    if not state_file.exists():
        return None
    active = json.loads(state_file.read_text(encoding="utf-8")).get("active")
    if not active:
        return None
    path = Path(data_dir) / active
    return path if path.exists() else None


def _set_active(data_dir, db_path):
    relative = Path(db_path).relative_to(data_dir).as_posix()
    _state_path(data_dir).write_text(
        json.dumps({"active": relative}, indent=2), encoding="utf-8"
    )


def create_event(data_dir, template, name, station_callsign):
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
        "dupe_key": template["dupe_key"],
        "contact_list": template.get("contact_list"),
        "export": template.get("export"),
    }
    conn = db.open_db(path)
    db.meta_set(conn, "event_uuid", event_uuid)
    db.meta_set(conn, "event_name", name)
    db.meta_set(conn, "station_callsign", station_callsign)
    db.meta_set(conn, "template_name", template["name"])
    db.meta_set(conn, "created_at", db.now_iso())
    db.meta_set(conn, "config", json.dumps(config))
    conn.close()

    _set_active(data_dir, path)
    return event_meta(path)


def event_meta(db_path):
    """Read identifying meta from an Event database file."""
    conn = db.open_db(db_path)
    try:
        return {
            "event_uuid": db.meta_get(conn, "event_uuid"),
            "name": db.meta_get(conn, "event_name"),
            "station_callsign": db.meta_get(conn, "station_callsign"),
            "template_name": db.meta_get(conn, "template_name"),
            "created_at": db.meta_get(conn, "created_at"),
        }
    finally:
        conn.close()


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
