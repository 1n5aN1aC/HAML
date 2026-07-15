"""End-to-end smoke test for the server core (milestone 1).

Stdlib only. Spawns the real server on a scratch port with a scratch data dir,
then walks the API as two simulated clients: event creation, push/pull, the
LWW conflict rule, tombstone sync, the cursor, backup, event switching, the
admin event listing, event-creation validation, the template editor (save,
fetch, delete, and an event created from a saved template), persistence
across a server restart, event deletion, and disk edge cases (broken
template files, stray and garbage dbs, a dangling state.json, state.json
pointing at a garbage db, and a corrupt state.json failing the boot).

Run: python server/tests/smoke.py   (uses sys.executable for the subprocess)
"""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
import api_rest    # noqa: E402  (server modules, exercised without the HTTP layer)
import db          # noqa: E402
import templates   # noqa: E402
# template ids this test may write into the live server/templates dir
TEST_TEMPLATE_IDS = ("smoke-scratch", "smoke-bad", "smoke-broken")
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ADMIN = {"X-Admin-Password": "test-pw"}

checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ok: {label}")


def remove_test_templates():
    """The server saves templates into the real server/templates dir, so
    make sure none of this test's files survive a run (or a killed one)."""
    for template_id in TEST_TEMPLATE_IDS:
        (SERVER_DIR / "templates" / f"{template_id}.json").unlink(missing_ok=True)


def cleanup(data_dir):
    """Remove the scratch dir, retrying briefly: on Windows the server's db
    file handles can outlive proc.wait() by a moment."""
    for _ in range(10):
        shutil.rmtree(data_dir, ignore_errors=True)
        if not data_dir.exists():
            return
        time.sleep(0.2)
    print(f"warning: could not remove {data_dir}")


def parse_json(method, path, status, raw):
    """Parse a response body, failing informatively on non-JSON (e.g. the
    plain-text 404 aiohttp serves for an unmatched route)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise AssertionError(f"FAIL: {method} {path} returned {status} "
                             f"with a non-JSON body: {raw[:200]!r}") from None


def request(method, path, body=None, headers=None):
    """Returns (status, parsed json)."""
    req = urllib.request.Request(BASE + path, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as resp:
            return resp.status, parse_json(method, path, resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, parse_json(method, path, exc.code, exc.read())


def request_raw(method, path, data, headers=None):
    """Like request(), but sends raw bytes (for non-JSON-body checks)."""
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, parse_json(method, path, resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, parse_json(method, path, exc.code, exc.read())


def iso(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).isoformat(timespec="milliseconds")


def make_contact(client_uuid, remote, edited_at, **overrides):
    contact = {
        "uuid": str(uuid.uuid4()),
        "qso_at": iso(),
        "last_edited": edited_at,
        "remote_callsign": remote,
        "operator_callsign": "KJ7ABC",
        "operator_initials": "JD",
        "client_uuid": client_uuid,
        "band": "20m",
        "mode": "Phone",
        "deleted": False,
        "section": "OR",  # built-in: top-level column, not a blob key
        "fields": {"class": "3A"},
    }
    contact.update(overrides)
    return contact


def start_server(config_path):
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "main.py"),
                             str(config_path)])
    for _ in range(50):  # wait for the server to come up
        time.sleep(0.1)
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited early (code {proc.returncode}) — "
                f"is port {PORT} already in use?")
        try:
            request("GET", "/api/event")
            return proc
        except (urllib.error.URLError, ConnectionError):
            continue
    stop_server(proc)
    raise AssertionError("server never came up")


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def expect_boot_failure(config_path, needle, label):
    """Start the server expecting it to die during boot; check that it exits
    nonzero and that its output mentions `needle` (so a crash for some other
    reason — port in use, import error — can't false-pass)."""
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "main.py"),
                             str(config_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        stop_server(proc)
        raise AssertionError(f"FAIL: {label} — server did not exit")
    check(proc.returncode != 0 and needle in out, label)


def unit_checks():
    """Direct-import checks for the built-in-fields plumbing that doesn't need
    the HTTP layer: the schema catch-up migration, validate_contact's handling
    of built-ins, and template validation of the new rules."""
    print("unit: built-in fields (no server):")
    tmp = Path(tempfile.mkdtemp(prefix="haml-unit-"))
    try:
        # opening a pre-change event db adds the built-in columns, with old rows kept and new columns defaulting to ''
        old_path = tmp / "old.db"
        raw = sqlite3.connect(old_path)
        raw.executescript(
            "CREATE TABLE contacts (uuid TEXT PRIMARY KEY, qso_at TEXT NOT NULL,"
            " created_at TEXT NOT NULL, last_edited TEXT NOT NULL,"
            " synced_at TEXT NOT NULL, remote_callsign TEXT NOT NULL,"
            " operator_callsign TEXT NOT NULL, operator_initials TEXT NOT NULL,"
            " client_uuid TEXT NOT NULL, band TEXT NOT NULL, mode TEXT NOT NULL,"
            " deleted INTEGER NOT NULL DEFAULT 0, fields TEXT NOT NULL DEFAULT '{}');")
        raw.execute(
            "INSERT INTO contacts (uuid, qso_at, created_at, last_edited,"
            " synced_at, remote_callsign, operator_callsign, operator_initials,"
            " client_uuid, band, mode, fields) VALUES"
            " ('old1','t','t','t','t','K7OLD','W7X','JD','cli','40m','SSB','{}')")
        raw.commit()
        raw.close()
        conn = db.open_db(old_path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
        check(all(f in cols for f in db.BUILTIN_FIELDS),
              "opening a pre-change event db adds the built-in columns")
        row = conn.execute("SELECT country FROM contacts WHERE uuid='old1'").fetchone()
        check(row["country"] == "", "migrated rows default new columns to ''")
        conn.close()

        # validate_contact keeps built-in values, defaults absent ones to '', and rejects non-strings
        base = {"uuid": "u", "qso_at": iso(), "last_edited": iso(),
                "remote_callsign": "VE3XYZ", "operator_callsign": "W7X",
                "operator_initials": "JD", "client_uuid": "c", "band": "20m",
                "mode": "CW", "deleted": False, "fields": {}}
        c = db.validate_contact(dict(base, country="Canada", cq_zone="5"))
        check(c["country"] == "Canada" and c["cq_zone"] == "5" and c["section"] == "",
              "validate_contact keeps built-ins and defaults absent ones to ''")
        try:
            db.validate_contact(dict(base, country=59))
            check(False, "non-string built-in should have raised")
        except ValueError:
            check(True, "validate_contact rejects a non-string built-in")

        # every label-less item (a built-in reference) in the stock templates names a built-in the server knows
        for tid in ("field-day", "pota", "generic", "example"):
            t = templates.load_template(tid)
            unknown = [f["name"] for f in t["fields"]
                       if "label" not in f and f["name"] not in db.BUILTIN_FIELDS]
            check(not unknown, f"{tid} built-in references are all known built-ins")

        # the client display registry's BUILTINS names mirror the server's BUILTIN_FIELDS (set equality only)
        registry = SERVER_DIR.parent / "client" / "src" / "builtin-fields.js"
        check(registry.is_file(), "client built-in registry file exists")
        body = re.search(r"export const BUILTINS = \{(.*?)\n\}",
                         registry.read_text(encoding="utf-8"), re.S)
        names = re.findall(r"^  (\w+): \{", body.group(1), re.M) if body else []
        check(names, "client registry parses (BUILTINS literal found)")
        check(sorted(names) == sorted(db.BUILTIN_FIELDS),
              "client BUILTINS names mirror server BUILTIN_FIELDS")

        # missing_required_fields reads built-ins top-level and customs from the blob, skipping history-only fields
        cfg = {
            "fields": [
                {"name": "class", "required": True, "entry": True},
                {"name": "section", "required": True, "entry": True},
                {"name": "their_park", "entry": True},
                {"name": "state", "required": True, "history": True},
            ],
        }
        ok_body = {"fields": {"class": "3A", "their_park": ""}, "section": "OR"}
        check(api_rest.missing_required_fields(cfg, ok_body) == [],
              "required check passes with custom and built-in values present")
        check(api_rest.missing_required_fields(cfg, {"fields": {}})
              == ["class", "section"],
              "required check reports blank custom (blob) and built-in"
              " (top-level) — and ignores history-only fields")

        # a required field with entry:false is not enforced at log time
        cfg_no_entry = {
            "fields": [
                {"name": "class", "required": True, "entry": False},
            ],
        }
        check(api_rest.missing_required_fields(cfg_no_entry, {"fields": {}}) == [],
              "a required field with entry:false is not enforced at log time")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    unit_checks()
    remove_test_templates()  # leftovers from a hard-killed earlier run
    data_dir = Path(tempfile.mkdtemp(prefix="haml-smoke-"))
    config_path = data_dir / "config.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": PORT,
        "data_dir": str(data_dir), "admin_password": "test-pw",
    }))

    proc = start_server(config_path)
    passed = False
    try:
        print("no active event:")
        # the event fetch is 404 before any event exists
        status, body = request("GET", "/api/event")
        check(status == 404 and body["error"] == "no active event",
              "GET /api/event is 404 before any event exists")

        # backup is 404 before any event exists
        status, _ = request("POST", "/api/admin/backup", headers=ADMIN)
        check(status == 404, "backup is 404 before any event exists")

        # a contact push is 404 before any event exists
        status, body = request("POST", "/api/contacts",
                               body=make_contact("client-A", "N0CALL", iso()))
        check(status == 404 and body["error"] == "no active event",
              "contact push is 404 before any event exists")

        # a contact pull is 404 before any event exists
        status, body = request("GET", "/api/contacts")
        check(status == 404 and body["error"] == "no active event",
              "contact pull is 404 before any event exists")

        # chat history is 404 before any event exists
        status, body = request("GET", "/api/chat")
        check(status == 404 and body["error"] == "no active event",
              "chat history is 404 before any event exists")

        # chat clear is 404 before any event exists
        status, body = request("DELETE", "/api/admin/chat", headers=ADMIN)
        check(status == 404 and body["error"] == "no active event",
              "chat clear is 404 before any event exists")

        print("admin auth:")
        # an admin endpoint rejects a missing password
        status, _ = request("GET", "/api/admin/templates")
        check(status == 401, "admin endpoint rejects a missing password")

        # the right password is accepted and the built-in templates are listed
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        check(status == 200, "admin endpoint accepts the password")
        ids = {t["id"] for t in body["templates"]}
        check({"field-day", "pota", "generic"} <= ids,
              "built-in templates are listed")

        # an admin endpoint rejects a wrong password
        status, _ = request("GET", "/api/admin/templates",
                            headers={"X-Admin-Password": "wrong-pw"})
        check(status == 401, "admin endpoint rejects a wrong password")

        # the event listing requires the password
        status, _ = request("GET", "/api/admin/events")
        check(status == 401, "event listing rejects a missing password")

        # event creation requires the password
        status, _ = request("POST", "/api/admin/events",
                            body={"template": "field-day", "name": "Nope",
                                  "station_callsign": "W7XYZ"})
        check(status == 401, "event creation rejects a missing password")

        # event activation requires the password
        status, _ = request("POST", f"/api/admin/events/{uuid.uuid4()}/activate",
                            body={})
        check(status == 401, "event activation rejects a missing password")

        # backup requires the password
        status, _ = request("POST", "/api/admin/backup")
        check(status == 401, "backup rejects a missing password")

        print("event listing before any event exists:")
        # the event listing starts empty
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200 and body["events"] == [],
              "event listing is empty before any event exists")

        print("template save:")
        # the scratch template used throughout the editor tests
        scratch = {
            "name": "Smoke Scratch",
            "fields": [
                {"name": "grid", "label": "Grid",
                 "required": True, "remember": True, "default": "",
                 "max_length": 4,
                 "validation": {"pattern": "[A-R]{2}\\d{2}",
                                "message": "Grid must look like CN85"},
                 "entry": True, "history": True},
            ],
            "bands": ["20m"], "modes": ["SSB"], "duplicate_type": "none",
            "export": None,
        }

        # template save requires the password
        status, _ = request("PUT", "/api/admin/templates/smoke-scratch",
                            body=scratch)
        check(status == 401, "template save rejects a missing password")

        # a valid template saves under its id and appears in the listing
        status, body = request("PUT", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN, body=scratch)
        check(status == 200 and body["id"] == "smoke-scratch",
              "valid template is saved")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        check(status == 200
              and "smoke-scratch" in {t["id"] for t in body["templates"]},
              "saved template appears in the listing")

        # overwriting an existing template replaces the stored copy
        status, body = request("PUT", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN,
                               body=dict(scratch, name="Smoke Scratch v2"))
        check(status == 200, "overwriting an existing template works")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        names = {t["id"]: t["name"] for t in body["templates"]}
        check(status == 200 and names["smoke-scratch"] == "Smoke Scratch v2",
              "overwrite replaced the stored template")

        # an unsafe (path-traversal) template id is rejected on save
        status, body = request("PUT", "/api/admin/templates/..%2Fevil",
                               headers=ADMIN, body=scratch)
        check(status == 400, "template save rejects an unsafe id")

        # a bad-charset template id is rejected on save
        status, body = request("PUT", "/api/admin/templates/My-Template",
                               headers=ADMIN, body=scratch)
        check(status == 400 and "template id" in body["error"],
              "template save rejects a bad-charset id")

        # a non-JSON save body is rejected
        status, body = request_raw("PUT", "/api/admin/templates/smoke-bad",
                                   b"not json", headers=ADMIN)
        check(status == 400 and body["error"] == "body must be JSON",
              "non-JSON template save body is rejected")

        print("template fetch (editor round trip):")
        # template fetch requires the password
        status, _ = request("GET", "/api/admin/templates/smoke-scratch")
        check(status == 401, "template fetch rejects a missing password")

        # a saved template round-trips through GET unchanged
        status, body = request("GET", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN)
        check(status == 200
              and body == dict(scratch, name="Smoke Scratch v2"),
              "saved template round-trips through GET unchanged")

        # fetching an unknown template is a 404
        status, _ = request("GET", "/api/admin/templates/no-such",
                            headers=ADMIN)
        check(status == 404, "fetching an unknown template is a 404")

        # an unsafe template id is a 404 on fetch
        status, _ = request("GET", "/api/admin/templates/..%2Fevil",
                            headers=ADMIN)
        check(status == 404, "template fetch rejects an unsafe id")

        # a validation block without a message is rejected
        no_message = json.loads(json.dumps(scratch))
        del no_message["fields"][0]["validation"]["message"]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=no_message)
        check(status == 400, "validation without a message is rejected")

        # a non-compiling validation pattern is rejected
        bad_pattern = json.loads(json.dumps(scratch))
        bad_pattern["fields"][0]["validation"]["pattern"] = "[unclosed"
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_pattern)
        check(status == 400, "non-compiling validation pattern is rejected")

        # a custom field without max_length is rejected
        no_length = json.loads(json.dumps(scratch))
        del no_length["fields"][0]["max_length"]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=no_length)
        check(status == 400, "field without max_length is rejected")

        # an unknown duplicate_type is rejected
        bad_dupe = dict(scratch, duplicate_type="callsign-prefix")
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_dupe)
        check(status == 400, "unknown duplicate_type is rejected")

        # a missing duplicate_type is rejected
        no_dupe = {k: v for k, v in scratch.items() if k != "duplicate_type"}
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=no_dupe)
        check(status == 400 and "duplicate_type" in body["error"],
              "template without a duplicate_type is rejected")

        # an unknown name with only entry/history is a custom field missing label/max_length
        bad_list = json.loads(json.dumps(scratch))
        bad_list["fields"] = [
            {"name": "nope", "entry": True, "history": True},
        ]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_list)
        check(status == 400 and "label" in body["error"],
              "custom field without label/max_length is rejected (unknown name"
              " with no def is just a custom field missing its keys)")

        # a built-in reference may not redefine 'label'
        builtin_with_label = json.loads(json.dumps(scratch))
        builtin_with_label["fields"] = [
            {"name": "section", "label": "Section", "entry": True, "history": True},
        ]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=builtin_with_label)
        check(status == 400 and "label" in body["error"],
              "built-in reference carrying 'label' is rejected")

        # a built-in reference may not redefine 'max_length'
        builtin_with_max = json.loads(json.dumps(scratch))
        builtin_with_max["fields"] = [
            {"name": "section", "max_length": 3, "entry": True, "history": True},
        ]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=builtin_with_max)
        check(status == 400 and "max_length" in body["error"],
              "built-in reference carrying 'max_length' is rejected")

        # a built-in reference may not redefine 'validation'
        builtin_with_validation = json.loads(json.dumps(scratch))
        builtin_with_validation["fields"] = [
            {"name": "section", "validation": {"pattern": "X", "message": "Y"},
             "entry": True, "history": True},
        ]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=builtin_with_validation)
        check(status == 400 and "validation" in body["error"],
              "built-in reference carrying 'validation' is rejected")

        # custom and built-in fields coexist in one ordered list (saved, then removed)
        builtin_valid = json.loads(json.dumps(scratch))
        builtin_valid["fields"] = [
            {"name": "grid", "label": "Grid", "max_length": 4,
             "entry": True, "history": True},
            {"name": "section", "entry": True, "history": True,
             "required": True, "default": "OR"},
        ]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=builtin_valid)
        check(status == 200,
              "custom and built-in fields coexist in the same ordered list")
        status, _ = request("DELETE", "/api/admin/templates/smoke-bad", headers=ADMIN)

        # a field without the 'entry' boolean is rejected
        missing_flag = json.loads(json.dumps(scratch))
        del missing_flag["fields"][0]["entry"]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=missing_flag)
        check(status == 400 and "entry" in body["error"],
              "field without 'entry' boolean is rejected")

        # a field without the 'history' boolean is rejected
        missing_flag2 = json.loads(json.dumps(scratch))
        del missing_flag2["fields"][0]["history"]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=missing_flag2)
        check(status == 400 and "history" in body["error"],
              "field without 'history' boolean is rejected")

        # a non-string field default is rejected
        bad_default = json.loads(json.dumps(scratch))
        bad_default["fields"][0]["default"] = 59
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_default)
        check(status == 400 and "default" in body["error"],
              "non-string field default is rejected")

        # a non-boolean 'remember' flag is rejected
        bad_remember = json.loads(json.dumps(scratch))
        bad_remember["fields"][0]["remember"] = "yes"
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_remember)
        check(status == 400 and "remember" in body["error"],
              "non-boolean field 'remember' is rejected")

        print("broken template file on disk:")
        # a broken template file on disk is skipped by the listing and 404s on fetch
        broken_path = SERVER_DIR / "templates" / "smoke-broken.json"
        broken_path.write_text("{not json", encoding="utf-8")
        try:
            status, body = request("GET", "/api/admin/templates",
                                   headers=ADMIN)
            check(status == 200
                  and "smoke-broken" not in {t["id"] for t in body["templates"]},
                  "broken template file is skipped by the listing")
            status, _ = request("GET", "/api/admin/templates/smoke-broken",
                                headers=ADMIN)
            check(status == 404, "fetching a broken template file is a 404")
        finally:
            broken_path.unlink()

        print("event creation validation:")
        # a known-good creation body the rejection tests mutate
        good = {"template": "field-day", "name": "Field Day 2026",
                "station_callsign": "W7XYZ"}

        # a missing event name is rejected
        without_name = {k: v for k, v in good.items() if k != "name"}
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=without_name)
        check(status == 400 and "name" in body["error"],
              "event creation without a name is rejected")

        # a blank event name is rejected
        status, _ = request("POST", "/api/admin/events", headers=ADMIN,
                            body=dict(good, name="   "))
        check(status == 400, "event creation with a blank name is rejected")

        # a missing station_callsign is rejected
        without_callsign = {k: v for k, v in good.items()
                            if k != "station_callsign"}
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=without_callsign)
        check(status == 400 and "station_callsign" in body["error"],
              "event creation without a station_callsign is rejected")

        # a blank station_callsign is rejected
        status, _ = request("POST", "/api/admin/events", headers=ADMIN,
                            body=dict(good, station_callsign="   "))
        check(status == 400,
              "event creation with a blank station_callsign is rejected")

        # an unknown template is rejected
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, template="no-such-template"))
        check(status == 400 and body["error"].startswith("bad template"),
              "event creation with an unknown template is rejected")

        # a location without a longitude is rejected
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, location={"latitude": 45.5}))
        check(status == 400 and "location" in body["error"],
              "event location without a longitude is rejected")

        # a non-numeric latitude is rejected
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, location={"latitude": "45.5",
                                                         "longitude": -122.6}))
        check(status == 400 and "latitude" in body["error"],
              "non-numeric event latitude is rejected")

        # an out-of-range latitude is rejected
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, location={"latitude": 91,
                                                         "longitude": -122.6}))
        check(status == 400 and "latitude" in body["error"],
              "out-of-range event latitude is rejected")

        # an out-of-range longitude is rejected
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, location={"latitude": 45.5,
                                                         "longitude": 181}))
        check(status == 400 and "longitude" in body["error"],
              "out-of-range event longitude is rejected")

        # a boolean latitude is rejected (bool is an int subclass)
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, location={"latitude": True,
                                                         "longitude": -122.6}))
        check(status == 400 and "latitude" in body["error"],
              "boolean event latitude is rejected")

        # a location with an extra key is rejected
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, location={"latitude": 45.5,
                                                         "longitude": -122.6,
                                                         "altitude": 30}))
        check(status == 400 and "location" in body["error"],
              "event location with an extra key is rejected")

        # a missing template is rejected
        without_template = {k: v for k, v in good.items() if k != "template"}
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=without_template)
        check(status == 400 and body["error"].startswith("bad template"),
              "event creation without a template is rejected")

        # a non-JSON creation body is rejected
        status, body = request_raw("POST", "/api/admin/events", b"not json",
                                   headers=ADMIN)
        check(status == 400 and body["error"] == "body must be JSON",
              "non-JSON event creation body is rejected")

        # the rejected attempts left no half-created event behind
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200 and body["events"] == [],
              "no half-created event leaked from the rejected attempts")
        status, _ = request("GET", "/api/event")
        check(status == 404, "still no active event after the rejected attempts")

        print("event creation:")
        # an event created from the field-day template becomes the active event
        status, created = request("POST", "/api/admin/events", headers=ADMIN,
                                  body={"template": "field-day",
                                        "name": "Field Day 2026",
                                        "station_callsign": "w7xyz",
                                        "local_exchange": "w7xyz 6a or",
                                        "location": {"latitude": 45.5,
                                                     "longitude": -122.6}})
        check(status == 201 and created["event_uuid"], "event created from template")
        status, event = request("GET", "/api/event")
        check(status == 200 and event["event_uuid"] == created["event_uuid"],
              "GET /api/event returns the new event")

        # the station callsign and local_exchange are uppercased on creation
        check(event["station_callsign"] == "W7XYZ", "station callsign uppercased")
        check(event["local_exchange"] == "W7XYZ 6A OR",
              "local_exchange round-trips uppercased")

        # the frozen config keeps the ordered fields list with its entry/history booleans
        field_names = [f["name"] for f in event["config"]["fields"]]
        check(field_names == ["class", "section"],
              "frozen config has the ordered fields list (custom + built-in ref)")
        check(event["config"]["fields"][0]["entry"] is True
              and event["config"]["fields"][0]["history"] is True
              and event["config"]["fields"][1]["entry"] is True
              and event["config"]["fields"][1]["history"] is True,
              "frozen config carries entry/history booleans on every field")

        # the frozen config carries validation, duplicate_type, remember, and location
        check(event["config"]["fields"][0]["validation"]["message"],
              "frozen config carries field validation")
        check(event["config"]["duplicate_type"] == "band-mode",
              "frozen config carries duplicate_type")
        check(event["config"]["fields"][0]["remember"] is True,
              "frozen config carries the remember flag")
        check(event["config"]["location"] == {"latitude": 45.5,
                                              "longitude": -122.6},
              "frozen config carries the creation-time location")

        print("event listing:")
        # the listing shows the new event with its meta and the active flag
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200 and len(body["events"]) == 1,
              "event listing shows the new event")
        listed = body["events"][0]
        check(listed["event_uuid"] == created["event_uuid"]
              and listed["name"] == "Field Day 2026"
              and listed["station_callsign"] == "W7XYZ"
              and listed["template_name"] == "ARRL Field Day",
              "listing carries the event's meta")
        check(listed["active"] is True, "the only event is marked active")

        print("event from a saved template + template delete:")
        # an event created from the editor-saved template freezes its config
        status, scratch_event = request("POST", "/api/admin/events",
                                        headers=ADMIN,
                                        body={"template": "smoke-scratch",
                                              "name": "Scratch Event",
                                              "station_callsign": "KJ7ABC"})
        check(status == 201, "event created from the editor-saved template")
        status, event = request("GET", "/api/event")
        config = event["config"]
        check([f["name"] for f in config["fields"]] == ["grid"]
              and config["fields"][0]["entry"] is True
              and config["fields"][0]["history"] is True
              and config["fields"][0]["remember"] is True
              and config["duplicate_type"] == "none",
              "frozen config mirrors the saved template (single fields list)")
        check(config["location"] is None,
              "event created without a location has none in its config")
        check(event["local_exchange"] is None,
              "event created without a local_exchange has none")

        # template delete requires the password
        status, _ = request("DELETE", "/api/admin/templates/smoke-scratch")
        check(status == 401, "template delete rejects a missing password")

        # a saved template can be deleted and disappears from the listing
        status, body = request("DELETE", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN)
        check(status == 200 and body["deleted"] == "smoke-scratch",
              "saved template can be deleted")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        check(status == 200
              and "smoke-scratch" not in {t["id"] for t in body["templates"]},
              "deleted template disappears from the listing")

        # deleting an unknown template is a 404
        status, _ = request("DELETE", "/api/admin/templates/smoke-scratch",
                            headers=ADMIN)
        check(status == 404, "deleting an unknown template is a 404")

        # an unsafe template id is a 404 on delete
        status, _ = request("DELETE", "/api/admin/templates/..%2Fevil",
                            headers=ADMIN)
        check(status == 404, "template delete rejects an unsafe id")

        # the event's frozen config survives its template's deletion
        status, event = request("GET", "/api/event")
        check(status == 200
              and [f["name"] for f in event["config"]["fields"]] == ["grid"],
              "event's frozen config survives its template's deletion")

        # restore the single-event state the rest of the walk expects
        status, _ = request("POST",
                            f"/api/admin/events/{created['event_uuid']}/activate",
                            headers=ADMIN, body={})
        check(status == 200, "field-day event re-activated")
        status, _ = request("DELETE",
                            f"/api/admin/events/{scratch_event['event_uuid']}",
                            headers=ADMIN)
        check(status == 200, "scratch event deleted")

        print("push/pull round trip (client A):")
        # a push stores a new contact; an identical re-push is a harmless upsert
        contact_a = make_contact("client-A", "N0CALL", iso(),
                                 country="United States", cq_zone="3",
                                 itu_zone="7", continent="NA")
        status, body = request("POST", "/api/contacts", body=contact_a)
        check(status == 200 and body["stored"], "push stores a new contact")
        status, body = request("POST", "/api/contacts", body=contact_a)
        check(status == 200 and body["stored"], "duplicate push is a harmless upsert")

        # a full pull returns the contact's blob fields and built-in columns
        status, body = request("GET", "/api/contacts")
        check(status == 200 and len(body["contacts"]) == 1
              and body["contacts"][0]["uuid"] == contact_a["uuid"]
              and body["contacts"][0]["fields"]["class"] == "3A",
              "full pull returns the contact with its JSON fields")
        check(body["contacts"][0]["section"] == "OR"
              and body["contacts"][0]["country"] == "United States"
              and body["contacts"][0]["cq_zone"] == "3"
              and body["contacts"][0]["continent"] == "NA",
              "built-in fields pull back as top-level columns")
        created_a = body["contacts"][0]["created_at"]

        # the pull's server_time is current — it doubles as the sync cursor
        skew = abs((datetime.fromisoformat(body["server_time"])
                    - datetime.now(timezone.utc)).total_seconds())
        check(skew < 10,
              "pull response server_time is close to the current time")
        cursor1 = body["server_time"]

        print("required enforcement on push:")
        # a push missing required values (built-in top-level, custom blob) is rejected
        blank = make_contact("client-A", "N0CALL", iso(), section="")
        del blank["fields"]["class"]
        status, body = request("POST", "/api/contacts", body=blank)
        check(status == 400 and "section" in body["error"]
              and "class" in body["error"],
              "contact missing a required built-in (top-level) or required"
              " custom (blob) is rejected at push time")

        print("LWW conflict (client B edits the same contact):")
        # the newer edit wins
        newer = dict(contact_a, operator_initials="XX",
                     last_edited=iso(+5), client_uuid="client-B")
        status, body = request("POST", "/api/contacts", body=newer)
        check(status == 200 and body["stored"], "newer edit wins")

        # the older edit is rejected
        older = dict(contact_a, operator_initials="ZZ", last_edited=iso(-3600))
        status, body = request("POST", "/api/contacts", body=older)
        check(status == 200 and not body["stored"],
              "older edit is rejected (LWW)")

        # the stored row is the LWW winner and keeps the original created_at
        status, body = request("GET", "/api/contacts")
        row = body["contacts"][0]
        check(status == 200
              and row["operator_initials"] == "XX"
              and row["client_uuid"] == "client-B",
              "stored row is the LWW winner, stamped with the last editor")
        check(row["created_at"] == created_a,
              "LWW overwrite preserves the original created_at")

        print("cursor semantics:")
        # a pull since the cursor returns the re-edited contact
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor1))
        check(status == 200
              and [c["uuid"] for c in body["contacts"]] == [contact_a["uuid"]],
              "pull since cursor returns the re-edited contact")

        # an old cursor sees both contacts, with created_at honored and built-ins ''
        b_created = iso(-7200)
        contact_b = make_contact("client-B", "W1AW", iso(), created_at=b_created)
        status, body = request("POST", "/api/contacts", body=contact_b)
        check(status == 200 and body["stored"], "second contact stored")
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor1))
        check(status == 200
              and {c["uuid"] for c in body["contacts"]}
              == {contact_a["uuid"], contact_b["uuid"]},
              "old cursor sees both changed contacts")
        row_b = next((c for c in body["contacts"]
                      if c["uuid"] == contact_b["uuid"]), None)
        check(row_b is not None and row_b["created_at"] == b_created,
              "client-supplied created_at is honored on first insert")
        check(row_b["country"] == "" and row_b["cq_zone"] == "",
              "a contact posted without built-in keys defaults them to ''")

        # an up-to-date cursor filters out unchanged rows
        time.sleep(0.002)  # step past the last write's millisecond ('>=' cursor)
        status, body = request("GET", "/api/contacts")
        cursor_now = body["server_time"]
        status, body = request("GET",
                               "/api/contacts?since=" + urllib.parse.quote(cursor_now))
        check(status == 200 and body["contacts"] == [],
              "up-to-date cursor returns no contacts")

        # after a re-edit the cursor returns only the changed contact
        touched = dict(contact_b, operator_initials="QQ", last_edited=iso(+5))
        status, body = request("POST", "/api/contacts", body=touched)
        check(status == 200 and body["stored"], "re-edit of the second contact stored")
        status, body = request("GET",
                               "/api/contacts?since=" + urllib.parse.quote(cursor_now))
        check(status == 200
              and [c["uuid"] for c in body["contacts"]] == [contact_b["uuid"]],
              "cursor pull returns only the re-edited contact, not the unchanged one")

        # a malformed 'since' timestamp is rejected
        status, body = request("GET", "/api/contacts?since=not-a-timestamp")
        check(status == 400 and "since" in body["error"],
              "bad 'since' timestamp is rejected")

        print("soft delete:")
        # a tombstone stores and syncs to clients pulling from an earlier (pull-derived) cursor
        status, body = request("GET", "/api/contacts")
        cursor2 = body["server_time"]
        tombstone = dict(newer, deleted=True, last_edited=iso(+10))
        status, body = request("POST", "/api/contacts", body=tombstone)
        check(status == 200 and body["stored"], "tombstone stored")
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor2))
        deleted_row = next((c for c in body["contacts"]
                            if c["uuid"] == contact_a["uuid"]), None)
        check(status == 200 and deleted_row is not None
              and deleted_row["deleted"] is True,
              "tombstone syncs to other clients")

        print("validation:")
        # an incomplete contact is rejected
        status, _ = request("POST", "/api/contacts",
                            body={"uuid": "x"})
        check(status == 400, "incomplete contact is rejected")

        # a non-JSON contact body is rejected
        status, body = request_raw("POST", "/api/contacts", b"not json")
        check(status == 400 and body["error"] == "body must be JSON",
              "non-JSON contact body is rejected")

        # a blank callsign is rejected
        bad = make_contact("client-A", "  ", iso())
        status, _ = request("POST", "/api/contacts", body=bad)
        check(status == 400, "blank callsign is rejected")

        # a JSON-array contact body is rejected
        status, body = request("POST", "/api/contacts", body=[1, 2, 3])
        check(status == 400 and body["error"] == "contact must be a JSON object",
              "JSON-array contact body is rejected")

        # an unparseable timestamp is rejected
        bad_ts = make_contact("client-A", "K7AAA", "not-a-timestamp")
        status, body = request("POST", "/api/contacts", body=bad_ts)
        check(status == 400 and body["error"].startswith("bad timestamp"),
              "unparseable timestamp is rejected")

        # a non-object fields value is rejected
        bad_fields = make_contact("client-A", "K7AAA", iso(),
                                  fields="not an object")
        status, body = request("POST", "/api/contacts", body=bad_fields)
        check(status == 400 and "fields" in body["error"],
              "non-object fields value is rejected")

        # absent required fields (custom and built-in) are rejected
        empty_fields = make_contact("client-A", "K7AAA", iso(), fields={})
        del empty_fields["section"]  # built-in absent entirely, not just blank
        status, body = request("POST", "/api/contacts", body=empty_fields)
        check(status == 400 and "class" in body["error"]
              and "section" in body["error"],
              "missing required fields (custom and built-in) are rejected")

        # a blank required built-in is rejected
        blank_builtin = make_contact("client-A", "K7AAA", iso(), section="  ")
        status, body = request("POST", "/api/contacts", body=blank_builtin)
        check(status == 400 and "section" in body["error"]
              and "class" not in body["error"],
              "blank required built-in field is rejected")

        # a blank required custom field is rejected
        blank_field = make_contact("client-A", "K7AAA", iso(),
                                   fields={"class": "  "})
        status, body = request("POST", "/api/contacts", body=blank_field)
        check(status == 400 and "class" in body["error"]
              and "section" not in body["error"],
              "blank required custom field is rejected")

        # a tombstone with empty fields still syncs (deletions are exempt)
        bare_tombstone = make_contact("client-A", "K7AAA", iso(),
                                      fields={}, section="", deleted=True)
        status, body = request("POST", "/api/contacts", body=bare_tombstone)
        check(status == 200 and body["stored"],
              "tombstone with empty fields still syncs")

        print("naive timestamp normalization:")
        # a timezone-less timestamp is accepted and stored as UTC
        naive = (datetime.now(timezone.utc).replace(tzinfo=None)
                 .isoformat(timespec="milliseconds"))
        naive_contact = make_contact("client-A", "K7BBB", naive)
        status, body = request("POST", "/api/contacts", body=naive_contact)
        check(status == 200 and body["stored"],
              "contact with a timezone-less timestamp is accepted")
        status, body = request("GET", "/api/contacts")
        row = next((c for c in body["contacts"]
                    if c["uuid"] == naive_contact["uuid"]), None)
        check(status == 200 and row is not None
              and row["last_edited"] == naive + "+00:00",
              "timezone-less timestamp is stored as UTC")

        print("backup + event switch:")
        # a backup lands in backups/ and is a working copy with contacts and meta
        status, body = request("POST", "/api/admin/backup", headers=ADMIN)
        check(status == 200
              and (data_dir / "backups" / body["backup"]).exists(),
              "backup file lands in backups/")
        backup_conn = sqlite3.connect(data_dir / "backups" / body["backup"])
        try:
            n_contacts = backup_conn.execute(
                "SELECT COUNT(*) FROM contacts").fetchone()[0]
            backup_name = backup_conn.execute(
                "SELECT value FROM meta WHERE key = 'event_name'").fetchone()[0]
        finally:
            backup_conn.close()
        check(n_contacts == 4 and backup_name == "Field Day 2026",
              "backup is a working copy with the contacts and meta")

        # creating a second event makes it the active one
        status, second = request("POST", "/api/admin/events", headers=ADMIN,
                                 body={"template": "pota",
                                       "name": "POTA Sunday",
                                       "station_callsign": "KJ7ABC"})
        check(status == 201, "second event created")
        status, event = request("GET", "/api/event")
        check(status == 200 and event["event_uuid"] == second["event_uuid"],
              "new event is now active")

        # the listing shows both events, active flag on the new one
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        active_flags = {e["event_uuid"]: e["active"] for e in body["events"]}
        check(status == 200
              and active_flags == {created["event_uuid"]: False,
                                   second["event_uuid"]: True},
              "listing shows both events with the active flag on the new one")

        # the new event starts with an empty log
        status, body = request("GET", "/api/contacts")
        check(status == 200 and body["contacts"] == [],
              "new event starts with an empty log")

        # activating a nonexistent event is a 404
        status, _ = request("POST",
                            f"/api/admin/events/{uuid.uuid4()}/activate",
                            headers=ADMIN, body={})
        check(status == 404, "activating a nonexistent event is a 404")

        # re-activating the old event restores its contacts and the active flag
        status, body = request("POST",
                               f"/api/admin/events/{created['event_uuid']}/activate",
                               headers=ADMIN, body={})
        check(status == 200, "old event re-activated")
        status, body = request("GET", "/api/contacts")
        check(status == 200 and len(body["contacts"]) == 4,
              "old event's contacts survived the switch")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        active_flags = {e["event_uuid"]: e["active"] for e in body["events"]}
        check(status == 200
              and active_flags == {created["event_uuid"]: True,
                                   second["event_uuid"]: False},
              "listing's active flag follows the re-activation")

        # the chat history endpoint answers on the active event
        status, body = request("GET", "/api/chat")
        check(status == 200 and body["messages"] == [], "chat history endpoint works")

        print("restart persistence:")
        # the active event, its meta/config, contacts, and the listing survive a restart
        stop_server(proc)
        proc = start_server(config_path)
        status, event = request("GET", "/api/event")
        check(status == 200 and event["event_uuid"] == created["event_uuid"],
              "active event survives a server restart")
        check(event["station_callsign"] == "W7XYZ"
              and [f["name"] for f in event["config"]["fields"]]
              == ["class", "section"]
              and event["config"]["fields"][0]["entry"] is True
              and event["config"]["fields"][0]["history"] is True
              and event["config"]["fields"][1]["entry"] is True
              and event["config"]["fields"][1]["history"] is True,
              "event meta and frozen config survive a restart")
        status, body = request("GET", "/api/contacts")
        check(status == 200 and len(body["contacts"]) == 4,
              "contacts survive a server restart")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200
              and {e["event_uuid"] for e in body["events"]}
              == {created["event_uuid"], second["event_uuid"]},
              "event listing is intact after a restart")

        print("event deletion:")
        # event delete requires the password
        status, _ = request("DELETE",
                            f"/api/admin/events/{second['event_uuid']}")
        check(status == 401, "event delete rejects a missing password")

        # deleting a nonexistent event is a 404
        status, _ = request("DELETE", f"/api/admin/events/{uuid.uuid4()}",
                            headers=ADMIN)
        check(status == 404, "deleting a nonexistent event is a 404")

        # deleting the active event is rejected
        status, body = request("DELETE",
                               f"/api/admin/events/{created['event_uuid']}",
                               headers=ADMIN)
        check(status == 400 and "active" in body["error"],
              "deleting the active event is rejected")

        # an inactive event deletes and disappears from the listing
        status, body = request("DELETE",
                               f"/api/admin/events/{second['event_uuid']}",
                               headers=ADMIN)
        check(status == 200 and body["deleted"] == second["event_uuid"],
              "inactive event can be deleted")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200
              and [e["event_uuid"] for e in body["events"]]
              == [created["event_uuid"]],
              "deleted event disappears from the listing")

        # deleting an already-deleted event is a 404
        status, _ = request("DELETE",
                            f"/api/admin/events/{second['event_uuid']}",
                            headers=ADMIN)
        check(status == 404, "deleting an already-deleted event is a 404")

        # the active event is untouched by the deletion
        status, event = request("GET", "/api/event")
        check(status == 200 and event["event_uuid"] == created["event_uuid"],
              "active event is untouched by the deletion")

        print("slug fallback:")
        # a name without alphanumerics falls back to the 'event' slug (create, verify, clean up)
        status, punct = request("POST", "/api/admin/events", headers=ADMIN,
                                body={"template": "pota", "name": "***",
                                      "station_callsign": "K7AAA"})
        check(status == 201
              and (data_dir / "events"
                   / f"event-{punct['event_uuid'][:8]}.db").exists(),
              "name without alphanumerics falls back to the 'event' slug")
        status, _ = request("POST",
                            f"/api/admin/events/{created['event_uuid']}/activate",
                            headers=ADMIN, body={})
        check(status == 200, "field-day event re-activated after the slug check")
        status, _ = request("DELETE",
                            f"/api/admin/events/{punct['event_uuid']}",
                            headers=ADMIN)
        check(status == 200, "slug-check event deleted")

        print("disk resilience:")
        # a stray db file without event meta is skipped by the listing
        (data_dir / "events" / "stray.db").write_bytes(b"")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200
              and [e["event_uuid"] for e in body["events"]]
              == [created["event_uuid"]],
              "stray db file without event meta is skipped by the listing")

        # a garbage (non-SQLite) db is skipped by the listing and doesn't break activation
        (data_dir / "events" / "garbage.db").write_bytes(
            b"\x00\x01 not a sqlite database, just junk bytes on disk \xff")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200
              and [e["event_uuid"] for e in body["events"]]
              == [created["event_uuid"]],
              "garbage db file is skipped by the listing")
        status, _ = request("POST",
                            f"/api/admin/events/{created['event_uuid']}/activate",
                            headers=ADMIN, body={})
        check(status == 200, "activation still works with a garbage db on disk")

        # state.json pointing at a garbage db boots with no active event
        stop_server(proc)
        (data_dir / "state.json").write_text(
            json.dumps({"active": "events/garbage.db"}))
        proc = start_server(config_path)
        status, body = request("GET", "/api/event")
        check(status == 404 and body["error"] == "no active event",
              "server boots with no active event when state.json points at"
              " a garbage db")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200
              and [e["event_uuid"] for e in body["events"]]
              == [created["event_uuid"]],
              "event listing still works with a garbage active db")

        # state.json pointing at a missing db file boots with no active event
        stop_server(proc)
        (data_dir / "state.json").write_text(
            json.dumps({"active": "events/no-such-file.db"}))
        proc = start_server(config_path)
        status, body = request("GET", "/api/event")
        check(status == 404 and body["error"] == "no active event",
              "server boots with no active event when state.json dangles")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200
              and [e["event_uuid"] for e in body["events"]]
              == [created["event_uuid"]],
              "event listing still works with a dangling state.json")

        # a corrupt state.json is the operator's to fix: boot must fail loudly, naming the file
        stop_server(proc)
        (data_dir / "state.json").write_text("{not json")
        expect_boot_failure(config_path, b"state.json",
                            "boot fails loudly on unparseable state.json")
        (data_dir / "state.json").write_text("[]")
        expect_boot_failure(config_path, b"state.json",
                            "boot fails loudly on wrong-shape state.json")

        print(f"\nPASS — {checks} checks")
        passed = True
    finally:
        stop_server(proc)
        remove_test_templates()
        if passed:
            cleanup(data_dir)
        else:
            print(f"keeping scratch dir for debugging: {data_dir}")


if __name__ == "__main__":
    main()
