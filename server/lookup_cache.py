"""Lookup cache: persistent SQLite store for upstream callsign-lookup results.

Lives in its own file (data/callook_cache.db), separate from any Event DB.
Schema is intentionally narrow: status + fetched_at + expires_at + payload
    This way, the only cache-row concept we ever need to evolve is what's inside `payload`
    (we add fields there as upstream sources expose them).

TTL policy:
  status='ok'         -> never expires. Information does not go stale for now.
  status='not_found'  -> 15min.
  status='error'      -> 5min. Upstream being down for hours is plausible;
                         a short TTL lets us discover recovery quickly.

The 408 long-poll ceiling does NOT write a cache row — clients are free to retry immediately.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS callook_cache (
  callsign    TEXT PRIMARY KEY,
  status      TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  payload     TEXT NOT NULL DEFAULT '{}',
  error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_callook_cache_expires
  ON callook_cache (expires_at);
"""

# Cache-row statuses — distinct from Callook's uppercase 'VALID'/'INVALID'.
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"

# TTLs (seconds). ok rows store expires_at = '' (never expires).
TTL_NOT_FOUND = 15 * 60
TTL_ERROR = 5 * 60

# get the current UTC timestamp in ISO 8601 format with milliseconds precision.
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

# normalizes a timestamp to UTC ISO 8601 with milliseconds precision, for comparison with now_iso().
def normalize_ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")

# sets the expires_at field for a cache row based on its status.
def _expires_at(status):
    if status == STATUS_OK:
        return ""
    seconds = TTL_NOT_FOUND if status == STATUS_NOT_FOUND else TTL_ERROR
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )

# opens the cache database file.
def open_cache(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

# gets a cache row for a given callsign.
# Honors TTL: an expired row is reported as if absent.
def get(conn, callsign):
    row = conn.execute(
        "SELECT * FROM callook_cache WHERE callsign = ?", (callsign,)
    ).fetchone()
    if row is None:
        return None
    expires = row["expires_at"]
    if expires and normalize_ts(expires) <= now_iso():
        return None
    return dict(row)

# inserts or updates a cache row for a given callsign.
def put(conn, callsign, status, payload, error=""):
    conn.execute(
        """INSERT INTO callook_cache
             (callsign, status, fetched_at, expires_at, payload, error)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(callsign) DO UPDATE SET
             status = excluded.status,
             fetched_at = excluded.fetched_at,
             expires_at = excluded.expires_at,
             payload = excluded.payload,
             error = excluded.error""",
        (
            callsign,
            status,
            now_iso(),
            _expires_at(status),
            json.dumps(payload),
            error,
        ),
    )
    conn.commit()

# deletes a cache row for a given callsign.
def purge_expired(conn):
    """Delete expired rows. Cheap; safe to call on a timer or after writes."""
    conn.execute(
        "DELETE FROM callook_cache WHERE expires_at != '' "
        "AND expires_at <= ?",
        (now_iso(),),
    )
    conn.commit()