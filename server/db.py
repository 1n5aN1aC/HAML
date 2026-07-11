"""Event database: schema, timestamps, and contact upsert/query logic.

One SQLite file per Event (ADR-0002). All timestamps are ISO-8601 UTC strings
normalized to the same format so they compare correctly as strings.

Two clocks live side by side (ADR-0001 + plan note):
  - last_edited  : the LWW conflict clock, supplied by whoever edited
  - synced_at    : server-stamped on every stored change; the pull cursor
"""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
  uuid              TEXT PRIMARY KEY,
  qso_at            TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  last_edited       TEXT NOT NULL,
  synced_at         TEXT NOT NULL,
  remote_callsign   TEXT NOT NULL,
  operator_callsign TEXT NOT NULL,
  operator_initials TEXT NOT NULL,
  client_uuid       TEXT NOT NULL,
  band              TEXT NOT NULL,
  mode              TEXT NOT NULL,
  deleted           INTEGER NOT NULL DEFAULT 0,
  fields            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_contacts_synced_at ON contacts (synced_at);
CREATE TABLE IF NOT EXISTS chat (
  uuid              TEXT PRIMARY KEY,
  sent_at           TEXT NOT NULL,
  operator_callsign TEXT NOT NULL,
  operator_initials TEXT NOT NULL,
  client_uuid       TEXT NOT NULL,
  text              TEXT NOT NULL
);
"""

# Client-supplied contact fields, in schema order. `fields` is a JSON object.
CONTACT_KEYS = [
    "uuid", "qso_at", "last_edited", "remote_callsign", "operator_callsign",
    "operator_initials", "client_uuid", "band", "mode", "deleted", "fields",
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_ts(value):
    """Normalize any ISO-8601 timestamp to our canonical UTC string form."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def validate_contact(body):
    """Return a normalized contact dict, or raise ValueError."""
    if not isinstance(body, dict):
        raise ValueError("contact must be a JSON object")
    missing = [k for k in CONTACT_KEYS if k not in body]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    contact = {}
    for key in ("uuid", "remote_callsign", "operator_callsign",
                "operator_initials", "client_uuid", "band", "mode"):
        value = body[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        contact[key] = value.strip()
    try:
        contact["qso_at"] = normalize_ts(body["qso_at"])
        contact["last_edited"] = normalize_ts(body["last_edited"])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"bad timestamp: {exc}") from None
    contact["deleted"] = 1 if body["deleted"] else 0
    if not isinstance(body["fields"], dict):
        raise ValueError("fields must be a JSON object")
    contact["fields"] = json.dumps(body["fields"])
    # created_at: optional from the client; preserved for existing rows in upsert
    contact["created_at"] = (
        normalize_ts(body["created_at"]) if body.get("created_at") else now_iso()
    )
    return contact


def upsert_contact(conn, contact):
    """LWW upsert (ADR-0001). Returns True if the row was stored, False if the
    incoming edit lost to a newer stored version."""
    row = conn.execute(
        "SELECT created_at, last_edited FROM contacts WHERE uuid = ?",
        (contact["uuid"],),
    ).fetchone()
    if row is not None:
        if contact["last_edited"] < row["last_edited"]:
            return False
        contact = dict(contact, created_at=row["created_at"])
    conn.execute(
        """INSERT OR REPLACE INTO contacts
           (uuid, qso_at, created_at, last_edited, synced_at, remote_callsign,
            operator_callsign, operator_initials, client_uuid, band, mode,
            deleted, fields)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contact["uuid"], contact["qso_at"], contact["created_at"],
         contact["last_edited"], now_iso(), contact["remote_callsign"],
         contact["operator_callsign"], contact["operator_initials"],
         contact["client_uuid"], contact["band"], contact["mode"],
         contact["deleted"], contact["fields"]),
    )
    conn.commit()
    return True


def contacts_since(conn, since=None):
    """All rows (including soft-deleted) with synced_at >= since, oldest first."""
    if since:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE synced_at >= ? ORDER BY synced_at",
            (normalize_ts(since),),
        )
    else:
        rows = conn.execute("SELECT * FROM contacts ORDER BY synced_at")
    return [contact_to_json(r) for r in rows]


def contact_to_json(row):
    contact = {key: row[key] for key in row.keys()}
    contact["fields"] = json.loads(contact["fields"])
    contact["deleted"] = bool(contact["deleted"])
    return contact


def chat_history(conn):
    rows = conn.execute("SELECT * FROM chat ORDER BY sent_at")
    return [{key: r[key] for key in r.keys()} for r in rows]


def insert_chat(conn, msg):
    """Idempotent insert (client-generated UUID; resends are harmless).
    Server stamps sent_at. Returns the stored row — the existing one when
    the UUID was already present."""
    conn.execute(
        """INSERT OR IGNORE INTO chat
           (uuid, sent_at, operator_callsign, operator_initials,
            client_uuid, text)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (msg["uuid"], now_iso(), msg["operator_callsign"],
         msg["operator_initials"], msg["client_uuid"], msg["text"]),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chat WHERE uuid = ?",
                       (msg["uuid"],)).fetchone()
    return {key: row[key] for key in row.keys()}


def clear_chat(conn):
    """Delete every chat message (admin action; no tombstones — clients
    replace their local history wholesale on refresh)."""
    conn.execute("DELETE FROM chat")
    conn.commit()
