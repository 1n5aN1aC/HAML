"""Smoke test for the automatic-backup decision (docs/SERVER.md).

The backup loop is time-driven, so instead of waiting on a timer this drives the
pure decision — backup_scheduler.maybe_backup(app) — directly, in-process. It
builds a real app against a scratch data dir (via main.build_app), then checks:
no contacts -> no backup; a new contact -> one backup; unchanged -> skip; another
contact -> a second backup.

Run: python server/tests/smoke_backup.py
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
import backup_scheduler  # noqa: E402
import db                # noqa: E402
import events            # noqa: E402
import lookup            # noqa: E402
import main              # noqa: E402
from config import load_config  # noqa: E402

checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ok: {label}")


def cleanup(app, data_dir):
    """Release the handles build_app opened, then remove the scratch dir,
    retrying briefly (Windows can hold db handles a moment longer)."""
    conn = app.get("conn")
    if conn is not None:
        conn.close()
    cache = app.get("lookup_cache")
    if cache is not None:
        cache.close()
    lookup.close(app)
    for _ in range(10):
        shutil.rmtree(data_dir, ignore_errors=True)
        if not data_dir.exists():
            return
        time.sleep(0.2)
    print(f"warning: could not remove {data_dir}")


def make_contact():
    """A minimal valid, normalized contact ready for db.upsert_contact."""
    now = db.now_iso()
    return db.validate_contact({
        "uuid": str(uuid.uuid4()),
        "qso_at": now,
        "last_edited": now,
        "remote_callsign": "W1AW",
        "operator_callsign": "K2ABC",
        "operator_initials": "AB",
        "client_uuid": str(uuid.uuid4()),
        "band": "20m",
        "mode": "SSB",
        "deleted": False,
        "fields": {},
    })


def backup_files(data_dir):
    return sorted((data_dir / "backups").glob("*.db"))


def main_test():
    data_dir = Path(tempfile.mkdtemp(prefix="haml-backup-"))
    config_path = data_dir / "config.json"
    config_path.write_text(
        json.dumps({"data_dir": data_dir.as_posix(),
                    "admin_password": "test-pw"}),
        encoding="utf-8",
    )
    cfg = load_config(config_path)

    # An active event must exist before build_app so it's opened as active.
    template = {"name": "Smoke", "fields": [], "bands": ["20m"],
                "modes": ["SSB"], "duplicate_type": "none"}
    events.create_event(cfg["data_dir"], template, "Backup Smoke", "K2ABC")

    app = main.build_app(cfg)
    try:
        conn = app["conn"]
        check(conn is not None, "active event opened by build_app")

        # No contacts yet -> nothing to back up.
        check(backup_scheduler.maybe_backup(app) is None,
              "empty event does not back up")
        check(backup_files(cfg["data_dir"]) == [], "no backup file written yet")

        # First contact -> one backup, a working copy carrying the contact.
        db.upsert_contact(conn, make_contact())
        path = backup_scheduler.maybe_backup(app)
        files = backup_files(cfg["data_dir"])
        check(path is not None and len(files) == 1,
              "a change writes exactly one backup")
        bconn = sqlite3.connect(files[0])
        try:
            n = bconn.execute(
                "SELECT COUNT(*) FROM contacts WHERE deleted = 0").fetchone()[0]
            name = bconn.execute(
                "SELECT value FROM meta WHERE key = 'event_name'").fetchone()[0]
        finally:
            bconn.close()
        check(n == 1 and name == "Backup Smoke",
              "backup is a working copy with the contact and meta")

        # Nothing changed -> skip (no new file).
        check(backup_scheduler.maybe_backup(app) is None,
              "unchanged event skips the backup")
        check(len(backup_files(cfg["data_dir"])) == 1,
              "skip wrote no additional file")

        # Second contact -> a second backup. (Timestamp is per-second; pause so
        # the filename stamp differs.)
        time.sleep(1.1)
        db.upsert_contact(conn, make_contact())
        check(backup_scheduler.maybe_backup(app) is not None,
              "a further change backs up again")
        check(len(backup_files(cfg["data_dir"])) == 2,
              "second change wrote a second file")

        print(f"\nAll {checks} checks passed.")
    finally:
        cleanup(app, data_dir)


if __name__ == "__main__":
    main_test()
