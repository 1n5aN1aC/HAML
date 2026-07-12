"""End-to-end smoke test for the server core (milestone 1).

Stdlib only. Spawns the real server on a scratch port with a scratch data dir,
then walks the API as two simulated clients: event creation, push/pull, the
LWW conflict rule, tombstone sync, the cursor, backup, and event switching.

Run: python server/tests/smoke.py   (uses sys.executable for the subprocess)
"""
import json
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


def request(method, path, body=None, headers=None):
    """Returns (status, parsed json)."""
    req = urllib.request.Request(BASE + path, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


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
        "fields": {"class": "3A", "section": "OR"},
    }
    contact.update(overrides)
    return contact


def main():
    data_dir = Path(tempfile.mkdtemp(prefix="haml-smoke-"))
    config_path = data_dir / "config.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": PORT,
        "data_dir": str(data_dir), "admin_password": "test-pw",
    }))

    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "main.py"),
                             str(config_path)])
    try:
        for _ in range(50):  # wait for the server to come up
            time.sleep(0.1)
            try:
                status, _ = request("GET", "/api/event")
                break
            except (urllib.error.URLError, ConnectionError):
                continue
        else:
            raise AssertionError("server never came up")

        print("no active event:")
        status, body = request("GET", "/api/event")
        check(status == 404 and body["error"] == "no active event",
              "GET /api/event is 404 before any event exists")
        status, _ = request("POST", "/api/admin/backup", headers=ADMIN)
        check(status == 404, "backup is 404 before any event exists")

        print("admin auth:")
        status, _ = request("GET", "/api/admin/templates")
        check(status == 401, "admin endpoint rejects a missing password")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        ids = {t["id"] for t in body["templates"]}
        check(status == 200 and {"field-day", "pota", "generic"} <= ids,
              "built-in templates are listed")

        print("template save + delete:")
        scratch = {
            "name": "Smoke Scratch",
            "fields": [
                {"name": "grid", "label": "Grid", "type": "text",
                 "required": True, "default": "", "order": 1,
                 "validation": {"pattern": "[A-R]{2}\\d{2}",
                                "message": "Grid must look like CN85"}},
            ],
            "bands": ["20m"], "modes": ["SSB"], "dupe_key": [],
            "contact_list": ["grid"], "export": None,
        }
        status, _ = request("PUT", "/api/admin/templates/smoke-scratch",
                            body=scratch)
        check(status == 401, "template save rejects a missing password")
        status, body = request("PUT", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN, body=scratch)
        check(status == 200 and body["id"] == "smoke-scratch",
              "valid template is saved")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        check("smoke-scratch" in {t["id"] for t in body["templates"]},
              "saved template appears in the listing")
        status, body = request("PUT", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN,
                               body=dict(scratch, name="Smoke Scratch v2"))
        check(status == 200, "overwriting an existing template works")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        names = {t["id"]: t["name"] for t in body["templates"]}
        check(names["smoke-scratch"] == "Smoke Scratch v2",
              "overwrite replaced the stored template")
        status, body = request("PUT", "/api/admin/templates/..%2Fevil",
                               headers=ADMIN, body=scratch)
        check(status == 400, "template save rejects an unsafe id")

        no_message = json.loads(json.dumps(scratch))
        del no_message["fields"][0]["validation"]["message"]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=no_message)
        check(status == 400, "validation without a message is rejected")
        bad_pattern = json.loads(json.dumps(scratch))
        bad_pattern["fields"][0]["validation"]["pattern"] = "[unclosed"
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_pattern)
        check(status == 400, "non-compiling validation pattern is rejected")
        bad_list = dict(scratch, contact_list=["nope"])
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_list)
        check(status == 400, "contact_list naming an unknown field is rejected")

        status, _ = request("DELETE", "/api/admin/templates/smoke-scratch")
        check(status == 401, "template delete rejects a missing password")
        status, body = request("DELETE", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN)
        check(status == 200 and body["deleted"] == "smoke-scratch",
              "saved template can be deleted")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        check("smoke-scratch" not in {t["id"] for t in body["templates"]},
              "deleted template disappears from the listing")
        status, _ = request("DELETE", "/api/admin/templates/smoke-scratch",
                            headers=ADMIN)
        check(status == 404, "deleting an unknown template is a 404")
        status, _ = request("DELETE", "/api/admin/templates/..%2Fevil",
                            headers=ADMIN)
        check(status == 404, "template delete rejects an unsafe id")

        print("event creation:")
        status, created = request("POST", "/api/admin/events", headers=ADMIN,
                                  body={"template": "field-day",
                                        "name": "Field Day 2026",
                                        "station_callsign": "w7xyz"})
        check(status == 201 and created["event_uuid"], "event created from template")
        status, event = request("GET", "/api/event")
        check(status == 200 and event["event_uuid"] == created["event_uuid"],
              "GET /api/event returns the new event")
        check(event["station_callsign"] == "W7XYZ", "station callsign uppercased")
        field_names = [f["name"] for f in event["config"]["fields"]]
        check(field_names == ["class", "section"], "frozen config has template fields")
        check(event["config"]["contact_list"] == ["class", "section"],
              "frozen config carries contact_list")
        check(event["config"]["fields"][0]["validation"]["message"],
              "frozen config carries field validation")

        print("push/pull round trip (client A):")
        contact_a = make_contact("client-A", "N0CALL", iso())
        status, body = request("POST", "/api/contacts", body=contact_a)
        check(status == 200 and body["stored"], "push stores a new contact")
        cursor0 = body["server_time"]
        status, body = request("POST", "/api/contacts", body=contact_a)
        check(status == 200 and body["stored"], "duplicate push is a harmless upsert")
        status, body = request("GET", "/api/contacts")
        check(len(body["contacts"]) == 1
              and body["contacts"][0]["uuid"] == contact_a["uuid"]
              and body["contacts"][0]["fields"]["section"] == "OR",
              "full pull returns the contact with its JSON fields")
        check(bool(datetime.fromisoformat(body["server_time"])),
              "pull response carries a parseable server_time")
        cursor1 = body["server_time"]

        print("LWW conflict (client B edits the same contact):")
        newer = dict(contact_a, operator_initials="XX",
                     last_edited=iso(+5), client_uuid="client-B")
        status, body = request("POST", "/api/contacts", body=newer)
        check(body["stored"], "newer edit wins")
        older = dict(contact_a, operator_initials="ZZ", last_edited=iso(-3600))
        status, body = request("POST", "/api/contacts", body=older)
        check(not body["stored"], "older edit is rejected (LWW)")
        status, body = request("GET", "/api/contacts")
        row = body["contacts"][0]
        check(row["operator_initials"] == "XX" and row["client_uuid"] == "client-B",
              "stored row is the LWW winner, stamped with the last editor")

        print("cursor semantics:")
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor1))
        check([c["uuid"] for c in body["contacts"]] == [contact_a["uuid"]],
              "pull since cursor returns the re-edited contact")
        contact_b = make_contact("client-B", "W1AW", iso())
        request("POST", "/api/contacts", body=contact_b)
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor0))
        check({c["uuid"] for c in body["contacts"]}
              == {contact_a["uuid"], contact_b["uuid"]},
              "old cursor sees both changed contacts")
        status, body = request("GET", "/api/contacts?since=not-a-timestamp")
        check(status == 400 and "since" in body["error"],
              "bad 'since' timestamp is rejected")

        print("soft delete:")
        # a real client's cursor comes from a pull response, never a push
        status, body = request("GET", "/api/contacts")
        cursor2 = body["server_time"]
        tombstone = dict(newer, deleted=True, last_edited=iso(+10))
        status, body = request("POST", "/api/contacts", body=tombstone)
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor2))
        deleted_row = next(c for c in body["contacts"]
                           if c["uuid"] == contact_a["uuid"])
        check(deleted_row["deleted"] is True, "tombstone syncs to other clients")

        print("validation:")
        status, _ = request("POST", "/api/contacts",
                            body={"uuid": "x"})
        check(status == 400, "incomplete contact is rejected")
        raw_req = urllib.request.Request(
            BASE + "/api/contacts", method="POST", data=b"not json",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(raw_req, timeout=5)
            raise AssertionError("FAIL: non-JSON body did not raise")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read())
        check(status == 400 and body["error"] == "body must be JSON",
              "non-JSON contact body is rejected")
        bad = make_contact("client-A", "  ", iso())
        status, _ = request("POST", "/api/contacts", body=bad)
        check(status == 400, "blank callsign is rejected")
        empty_fields = make_contact("client-A", "K7AAA", iso(), fields={})
        status, body = request("POST", "/api/contacts", body=empty_fields)
        check(status == 400 and "class" in body["error"]
              and "section" in body["error"],
              "missing required template fields are rejected")
        blank_field = make_contact("client-A", "K7AAA", iso(),
                                   fields={"class": "3A", "section": "  "})
        status, body = request("POST", "/api/contacts", body=blank_field)
        check(status == 400 and "section" in body["error"]
              and "class" not in body["error"],
              "blank required template field is rejected")
        bare_tombstone = make_contact("client-A", "K7AAA", iso(),
                                      fields={}, deleted=True)
        status, body = request("POST", "/api/contacts", body=bare_tombstone)
        check(status == 200 and body["stored"],
              "tombstone with empty fields still syncs")

        print("backup + event switch:")
        status, body = request("POST", "/api/admin/backup", headers=ADMIN)
        check(status == 200
              and (data_dir / "backups" / body["backup"]).exists(),
              "backup file lands in backups/")
        status, second = request("POST", "/api/admin/events", headers=ADMIN,
                                 body={"template": "pota",
                                       "name": "POTA Sunday",
                                       "station_callsign": "KJ7ABC"})
        check(status == 201, "second event created")
        status, event = request("GET", "/api/event")
        check(event["event_uuid"] == second["event_uuid"],
              "new event is now active")
        status, body = request("GET", "/api/contacts")
        check(body["contacts"] == [], "new event starts with an empty log")
        status, _ = request("POST",
                            f"/api/admin/events/{uuid.uuid4()}/activate",
                            headers=ADMIN, body={})
        check(status == 404, "activating a nonexistent event is a 404")
        status, body = request("POST",
                               f"/api/admin/events/{created['event_uuid']}/activate",
                               headers=ADMIN, body={})
        check(status == 200, "old event re-activated")
        status, body = request("GET", "/api/contacts")
        check(len(body["contacts"]) == 3, "old event's contacts survived the switch")

        status, body = request("GET", "/api/chat")
        check(status == 200 and body["messages"] == [], "chat history endpoint works")

        print(f"\nPASS — {checks} checks")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
