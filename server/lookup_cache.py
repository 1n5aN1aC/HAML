"""Lookup cache: persistent SQLite store for upstream callsign-lookup results.

Lives in its own DB (data/lookup_cache.db).
Schema is intentionally narrow: status + fetched_at + expires_at + source + payload.
The only cache-row concept we ever need to evolve is what's inside `payload`:
`payload` now follows the strict canonical record defined in `lookup_record`.
Future providers (QRZ, HamQTH) just need an adapter, not a new cache shape.

TTL policy:
  status='ok', clean    -> 365 days. Information does not go stale.
  status='ok', dirty    -> 15 min.  At least one field failed coercion;
                                     the row is best-effort; retry sooner.
  status='not_found'    -> 30 days. A non-existent callsign stays non-existent.
  status='error'        -> 15 min.  Upstream being down for hours is plausible;
                                     a short TTL lets us discover recovery quickly.

The 408 long-poll ceiling does NOT write a cache row — clients are free to
retry immediately.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

# lookup_record is the canonical home of the timestamp format.
from lookup_record import now_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS lookup_cache (
  callsign    TEXT PRIMARY KEY,
  status      TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT '',
  payload     TEXT NOT NULL DEFAULT '{}',
  error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lookup_cache_expires
  ON lookup_cache (expires_at);
"""

# Cache-row statuses — distinct from Callook's uppercase 'VALID'/'INVALID'.
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"

# TTLs (seconds).
TTL_OK = 365 * 24 * 60 * 60        # 1 year — info doesn't go stale
TTL_NOT_FOUND = 30 * 24 * 60 * 60  # 1 month — 30 fixed days
TTL_ERROR = 15 * 60                # 15 minutes — short enough to recover quickly

# Normalize a timestamp to UTC ISO 8601 with milliseconds, for comparison with now_iso(). Used when reading back expires_at.
def normalize_ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")

# Compute expires_at for a cache row. Always returns a timestamp: the old "expires_at = '' means never expire" convention is gone;
# ok rows now have a 365-day TTL (or 15 min if dirty).
def _expires_at(status, dirty=False):
    seconds = TTL_OK
    if status == STATUS_NOT_FOUND:
        seconds = TTL_NOT_FOUND
    elif status == STATUS_ERROR or (status == STATUS_OK and dirty):
        seconds = TTL_ERROR
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )

# Open the cache database file.
def open_cache(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

# Get a cache row for a given callsign. Honors TTL: an expired row is
# reported as if absent.
def get(conn, callsign):
    row = conn.execute(
        "SELECT * FROM lookup_cache WHERE callsign = ?", (callsign,)
    ).fetchone()
    if row is None:
        return None
    # expires_at is always populated now, so the old "if expires:" guard collapses into a direct comparison.
    if normalize_ts(row["expires_at"]) <= now_iso():
        return None
    return dict(row)

# Insert or update a cache row for a given callsign.
# This is the single place record metadata is stamped:
def put(conn, callsign, status, payload, error="", source="", dirty=False):
    fetched = now_iso()
    record = dict(payload)
    record["fetched_at"] = fetched
    if status == STATUS_OK:
        record["source"] = source
    conn.execute(
        """INSERT INTO lookup_cache
             (callsign, status, fetched_at, expires_at, source, payload, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(callsign) DO UPDATE SET
             status = excluded.status,
             fetched_at = excluded.fetched_at,
             expires_at = excluded.expires_at,
             source = excluded.source,
             payload = excluded.payload,
             error = excluded.error""",
        (
            callsign,
            status,
            fetched,
            _expires_at(status, dirty),
            source,
            json.dumps(record),
            error,
        ),
    )
    conn.commit()
    return record

# Delete expired rows. Cheap; safe to call on a timer or after writes.
def purge_expired(conn):
    conn.execute(
        "DELETE FROM lookup_cache WHERE expires_at <= ?",
        (now_iso(),),
    )
    conn.commit()


# Raw per-status row counts (expired rows included — this reports actual DB
# contents, which is what the admin Clear button acts on). Do not "fix" by
# filtering on expires_at.
def stats(conn):
    counts = {STATUS_OK: 0, STATUS_NOT_FOUND: 0, STATUS_ERROR: 0}
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM lookup_cache GROUP BY status"
    ):
        if status in counts:
            counts[status] = n
    return counts


# Delete every cache row. Returns the number of rows removed.
def clear(conn):
    deleted = conn.execute("DELETE FROM lookup_cache").rowcount
    conn.commit()
    return deleted