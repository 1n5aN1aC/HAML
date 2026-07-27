"""The shared read-only handle on `lookup_data.sqlite`.

One sqlite file holds every offline lookup dataset — `fcc_operators`,
`ca_operators`, and the region tables — so the adapters that read it share a
single connection rather than opening the same file twice. This module owns
that handle: opening it, warning when it looks stale, and closing it.

Both `setup()` and `close()` are idempotent and keyed on the app dict, so
every source that needs the DB can call them from its own setup/close without
knowing whether another source got there first. That keeps the source-module
contract in `lookup.py` intact: no source depends on another's position in
the chain.

Never raises: an unopenable file leaves `app["lookup_db"]` as None and each
adapter degrades to a STATUS_ERROR result, so the server still boots and the
chain still falls through to the prefix DB.
"""
import os
import sqlite3
import time

# --- open the read-only DB connection ---------------------------------------
# `uri=True` + `mode=ro` is the official way to open a sqlite read-only via a file: URI.
def _open(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # Row factory so the adapters can read by column name in _build_record().
    # Default tuples would force them to track ordinal positions, which is
    # fragile against importer-side column reordering.
    conn.row_factory = sqlite3.Row
    return conn

# Staleness warning: when the DB is older than the configured threshold.
# The datasets refresh on their own upstream cadences and the schema carries
# no build timestamp, so the file's mtime is the proxy for its build date.
def _warn_if_stale(db_path, max_age_days):
    if not max_age_days:  # 0 disables the check
        return
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        return  # File-age unknowable; the open path already warns on a bad file.
    age_days = (time.time() - mtime) / 86400
    if age_days > max_age_days:
        print(
            f"warning: lookup dataset at {db_path} is {age_days:.1f} days old "
            f"(threshold {max_age_days}); the FCC ULS dump refreshes weekly "
            "and the ISED list regularly, consider rebuilding it"
        )

# setup(): called from each adapter's setup(), which lookup.setup() drives.
# Idempotent — the second caller sees the handle the first one opened.
# Missing/unopenable -> warn, store None. We never raise; the server must
# boot so the admin endpoints still work.
def setup(app):
    if app.get("lookup_db") is not None:
        return
    db_path = app["cfg"]["lookup_db_path"]
    app["lookup_db_path"] = str(db_path)
    try:
        conn = _open(db_path)
        # Force a real open + pragma so a corrupt file fails here, not on the first lookup.
        conn.execute("PRAGMA quick_check").fetchone()
        app["lookup_db"] = conn
        _warn_if_stale(db_path, app["cfg"].get("lookup_db_max_age_days", 0))
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        # Not fatal: the chain falls through to the prefix DB.
        print(
            f"warning: lookup dataset unavailable at {db_path} ({exc}); "
            "Falling back to other sources"
        )
        app["lookup_db"] = None

# close(): called from each adapter's close() at shutdown, so it too is
# idempotent. The read-only handle is process-lived otherwise; closing it lets
# a test's scratch dir be removed.
def close(app):
    conn = app.get("lookup_db")
    if conn is not None:
        conn.close()
        app["lookup_db"] = None
