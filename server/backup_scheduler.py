"""Automatic backup of the active Event (docs/SERVER.md).

A background task ticks every `auto_backup_interval_minutes` and snapshots the
active Event via `events.backup_event` — but only when contacts have changed
since the last snapshot, so a quiet stretch writes nothing. The change signal is
MAX(synced_at) over the contacts table (server-stamped on every stored contact
change), compared against an in-memory marker that resets whenever the active
connection changes (api_rest.set_active_connection). On restart or event switch
the marker starts empty, so the first tick snapshots once if the event has any
contacts, then stays quiet until the next change.

Retention is deliberately unmanaged: files accumulate only while logging is
active, and the operator prunes data/backups/ by hand.
"""
import asyncio
import events


def _max_synced_at(conn):
    """Newest synced_at across all contacts, or None for an empty event."""
    row = conn.execute("SELECT MAX(synced_at) AS m FROM contacts").fetchone()
    return row["m"] if row else None

def maybe_backup(app):
    """Snapshot the active Event iff contacts changed since the last snapshot.
    Returns the backup path when one was written, else None. Directly callable
    (the loop and the tests both go through here)."""
    conn = app.get("conn")
    if conn is None:  # no active event
        return None
    marker = _max_synced_at(conn)
    if marker is None or marker == app.get("last_backup_marker"):
        return None  # nothing logged, or unchanged since last snapshot
    path = events.backup_event(conn, app["cfg"]["data_dir"], app["event"]["name"])
    app["last_backup_marker"] = marker
    print(f"auto-backup: {path.name}")
    return path

async def _loop(app, interval_seconds):
    """Tick forever; a failed snapshot is logged but never kills the loop."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            maybe_backup(app)
        except Exception as exc:  # disk full, DB locked, ... — keep ticking
            print(f"auto-backup failed: {exc}")

def setup(app):
    """Start the automatic-backup loop unless disabled (interval <= 0)."""
    minutes = app["cfg"]["auto_backup_interval_minutes"]
    if not minutes or minutes <= 0:
        print("auto-backup disabled")
        return

    async def _start(app):
        app["backup_task"] = asyncio.create_task(_loop(app, minutes * 60))

    async def _stop(app):
        task = app.get("backup_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)