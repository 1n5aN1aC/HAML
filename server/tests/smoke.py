"""End-to-end smoke test for the server core (milestone 1).

Stdlib only. Spawns the real server on a scratch port with a scratch data dir,
then walks the API as two simulated clients: event creation, push/pull, the
LWW conflict rule, tombstone sync, the cursor, backup, event switching, the
admin event listing, event-creation validation, persistence across a
server restart, and event deletion.

Run: python server/tests/smoke.py   (uses sys.executable for the subprocess)
"""
import json
import shutil
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


def cleanup(data_dir):
    """Remove the scratch dir, retrying briefly: on Windows the server's db
    file handles can outlive proc.wait() by a moment."""
    for _ in range(10):
        shutil.rmtree(data_dir, ignore_errors=True)
        if not data_dir.exists():
            return
        time.sleep(0.2)
    print(f"warning: could not remove {data_dir}")


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


def main():
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
        status, body = request("GET", "/api/event")
        check(status == 404 and body["error"] == "no active event",
              "GET /api/event is 404 before any event exists")
        status, _ = request("POST", "/api/admin/backup", headers=ADMIN)
        check(status == 404, "backup is 404 before any event exists")
        status, body = request("POST", "/api/contacts",
                               body=make_contact("client-A", "N0CALL", iso()))
        check(status == 404 and body["error"] == "no active event",
              "contact push is 404 before any event exists")
        status, body = request("GET", "/api/contacts")
        check(status == 404 and body["error"] == "no active event",
              "contact pull is 404 before any event exists")
        status, body = request("GET", "/api/chat")
        check(status == 404 and body["error"] == "no active event",
              "chat history is 404 before any event exists")
        status, body = request("DELETE", "/api/admin/chat", headers=ADMIN)
        check(status == 404 and body["error"] == "no active event",
              "chat clear is 404 before any event exists")

        print("admin auth:")
        status, _ = request("GET", "/api/admin/templates")
        check(status == 401, "admin endpoint rejects a missing password")
        status, body = request("GET", "/api/admin/templates", headers=ADMIN)
        check(status == 200, "admin endpoint accepts the password")
        ids = {t["id"] for t in body["templates"]}
        check({"field-day", "pota", "generic"} <= ids,
              "built-in templates are listed")
        status, _ = request("GET", "/api/admin/templates",
                            headers={"X-Admin-Password": "wrong-pw"})
        check(status == 401, "admin endpoint rejects a wrong password")
        status, _ = request("GET", "/api/admin/events")
        check(status == 401, "event listing rejects a missing password")
        status, _ = request("POST", "/api/admin/events",
                            body={"template": "field-day", "name": "Nope",
                                  "station_callsign": "W7XYZ"})
        check(status == 401, "event creation rejects a missing password")
        status, _ = request("POST", f"/api/admin/events/{uuid.uuid4()}/activate",
                            body={})
        check(status == 401, "event activation rejects a missing password")
        status, _ = request("POST", "/api/admin/backup")
        check(status == 401, "backup rejects a missing password")

        print("event listing before any event exists:")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(status == 200 and body["events"] == [],
              "event listing is empty before any event exists")

        print("template save + delete:")
        scratch = {
            "name": "Smoke Scratch",
            "fields": [
                {"name": "grid", "label": "Grid", "type": "text",
                 "required": True, "default": "", "max_length": 4, "order": 1,
                 "validation": {"pattern": "[A-R]{2}\\d{2}",
                                "message": "Grid must look like CN85"}},
            ],
            "bands": ["20m"], "modes": ["SSB"], "duplicate_type": "none",
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

        print("template fetch (editor round trip):")
        status, _ = request("GET", "/api/admin/templates/smoke-scratch")
        check(status == 401, "template fetch rejects a missing password")
        status, body = request("GET", "/api/admin/templates/smoke-scratch",
                               headers=ADMIN)
        check(status == 200
              and body == dict(scratch, name="Smoke Scratch v2"),
              "saved template round-trips through GET unchanged")
        status, _ = request("GET", "/api/admin/templates/no-such",
                            headers=ADMIN)
        check(status == 404, "fetching an unknown template is a 404")
        status, _ = request("GET", "/api/admin/templates/..%2Fevil",
                            headers=ADMIN)
        check(status == 404, "template fetch rejects an unsafe id")

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
        no_length = json.loads(json.dumps(scratch))
        del no_length["fields"][0]["max_length"]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=no_length)
        check(status == 400, "text field without max_length is rejected")
        bad_dupe = dict(scratch, duplicate_type="callsign-prefix")
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_dupe)
        check(status == 400, "unknown duplicate_type is rejected")
        bad_list = dict(scratch, contact_list=["nope"])
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_list)
        check(status == 400, "contact_list naming an unknown field is rejected")
        no_order = json.loads(json.dumps(scratch))
        del no_order["fields"][0]["order"]
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=no_order)
        check(status == 400 and "order" in body["error"],
              "field without an order is rejected")
        bad_default = json.loads(json.dumps(scratch))
        bad_default["fields"][0]["default"] = 59
        status, body = request("PUT", "/api/admin/templates/smoke-bad",
                               headers=ADMIN, body=bad_default)
        check(status == 400 and "default" in body["error"],
              "non-string field default is rejected")

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

        print("event creation validation:")
        good = {"template": "field-day", "name": "Field Day 2026",
                "station_callsign": "W7XYZ"}
        without_name = {k: v for k, v in good.items() if k != "name"}
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=without_name)
        check(status == 400 and "name" in body["error"],
              "event creation without a name is rejected")
        status, _ = request("POST", "/api/admin/events", headers=ADMIN,
                            body=dict(good, name="   "))
        check(status == 400, "event creation with a blank name is rejected")
        without_callsign = {k: v for k, v in good.items()
                            if k != "station_callsign"}
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=without_callsign)
        check(status == 400 and "station_callsign" in body["error"],
              "event creation without a station_callsign is rejected")
        status, _ = request("POST", "/api/admin/events", headers=ADMIN,
                            body=dict(good, station_callsign="   "))
        check(status == 400,
              "event creation with a blank station_callsign is rejected")
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=dict(good, template="no-such-template"))
        check(status == 400 and body["error"].startswith("bad template"),
              "event creation with an unknown template is rejected")
        without_template = {k: v for k, v in good.items() if k != "template"}
        status, body = request("POST", "/api/admin/events", headers=ADMIN,
                               body=without_template)
        check(status == 400 and body["error"].startswith("bad template"),
              "event creation without a template is rejected")
        raw_req = urllib.request.Request(
            BASE + "/api/admin/events", method="POST", data=b"not json",
            headers={"Content-Type": "application/json", **ADMIN})
        try:
            urllib.request.urlopen(raw_req, timeout=5)
            raise AssertionError("FAIL: non-JSON event body did not raise")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read())
        check(status == 400 and body["error"] == "body must be JSON",
              "non-JSON event creation body is rejected")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check(body["events"] == [],
              "no half-created event leaked from the rejected attempts")
        status, _ = request("GET", "/api/event")
        check(status == 404, "still no active event after the rejected attempts")

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

        print("event listing:")
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

        print("push/pull round trip (client A):")
        contact_a = make_contact("client-A", "N0CALL", iso())
        status, body = request("POST", "/api/contacts", body=contact_a)
        check(status == 200 and body["stored"], "push stores a new contact")
        status, body = request("POST", "/api/contacts", body=contact_a)
        check(status == 200 and body["stored"], "duplicate push is a harmless upsert")
        status, body = request("GET", "/api/contacts")
        check(len(body["contacts"]) == 1
              and body["contacts"][0]["uuid"] == contact_a["uuid"]
              and body["contacts"][0]["fields"]["section"] == "OR",
              "full pull returns the contact with its JSON fields")
        skew = abs((datetime.fromisoformat(body["server_time"])
                    - datetime.now(timezone.utc)).total_seconds())
        check(skew < 10,
              "pull response server_time is close to the current time")
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
        status, body = request("POST", "/api/contacts", body=contact_b)
        check(status == 200 and body["stored"], "second contact stored")
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor1))
        check({c["uuid"] for c in body["contacts"]}
              == {contact_a["uuid"], contact_b["uuid"]},
              "old cursor sees both changed contacts")
        # exclusion: an up-to-date cursor must filter out unchanged rows
        time.sleep(0.002)  # step past the last write's millisecond ('>=' cursor)
        status, body = request("GET", "/api/contacts")
        cursor_now = body["server_time"]
        status, body = request("GET",
                               "/api/contacts?since=" + urllib.parse.quote(cursor_now))
        check(body["contacts"] == [],
              "up-to-date cursor returns no contacts")
        touched = dict(contact_b, operator_initials="QQ", last_edited=iso(+5))
        status, body = request("POST", "/api/contacts", body=touched)
        check(status == 200 and body["stored"], "re-edit of the second contact stored")
        status, body = request("GET",
                               "/api/contacts?since=" + urllib.parse.quote(cursor_now))
        check([c["uuid"] for c in body["contacts"]] == [contact_b["uuid"]],
              "cursor pull returns only the re-edited contact, not the unchanged one")
        status, body = request("GET", "/api/contacts?since=not-a-timestamp")
        check(status == 400 and "since" in body["error"],
              "bad 'since' timestamp is rejected")

        print("soft delete:")
        # a real client's cursor comes from a pull response, never a push
        status, body = request("GET", "/api/contacts")
        cursor2 = body["server_time"]
        tombstone = dict(newer, deleted=True, last_edited=iso(+10))
        status, body = request("POST", "/api/contacts", body=tombstone)
        check(status == 200 and body["stored"], "tombstone stored")
        status, body = request("GET", "/api/contacts?since=" + urllib.parse.quote(cursor2))
        deleted_row = next((c for c in body["contacts"]
                            if c["uuid"] == contact_a["uuid"]), None)
        check(deleted_row is not None and deleted_row["deleted"] is True,
              "tombstone syncs to other clients")

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
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        active_flags = {e["event_uuid"]: e["active"] for e in body["events"]}
        check(active_flags == {created["event_uuid"]: False,
                               second["event_uuid"]: True},
              "listing shows both events with the active flag on the new one")
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
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        active_flags = {e["event_uuid"]: e["active"] for e in body["events"]}
        check(active_flags == {created["event_uuid"]: True,
                               second["event_uuid"]: False},
              "listing's active flag follows the re-activation")

        status, body = request("GET", "/api/chat")
        check(status == 200 and body["messages"] == [], "chat history endpoint works")

        print("restart persistence:")
        stop_server(proc)
        proc = start_server(config_path)
        status, event = request("GET", "/api/event")
        check(status == 200 and event["event_uuid"] == created["event_uuid"],
              "active event survives a server restart")
        check(event["station_callsign"] == "W7XYZ"
              and [f["name"] for f in event["config"]["fields"]]
              == ["class", "section"],
              "event meta and frozen config survive a restart")
        status, body = request("GET", "/api/contacts")
        check(len(body["contacts"]) == 3, "contacts survive a server restart")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check({e["event_uuid"] for e in body["events"]}
              == {created["event_uuid"], second["event_uuid"]},
              "event listing is intact after a restart")

        print("event deletion:")
        status, _ = request("DELETE",
                            f"/api/admin/events/{second['event_uuid']}")
        check(status == 401, "event delete rejects a missing password")
        status, _ = request("DELETE", f"/api/admin/events/{uuid.uuid4()}",
                            headers=ADMIN)
        check(status == 404, "deleting a nonexistent event is a 404")
        status, body = request("DELETE",
                               f"/api/admin/events/{created['event_uuid']}",
                               headers=ADMIN)
        check(status == 400 and "active" in body["error"],
              "deleting the active event is rejected")
        status, body = request("DELETE",
                               f"/api/admin/events/{second['event_uuid']}",
                               headers=ADMIN)
        check(status == 200 and body["deleted"] == second["event_uuid"],
              "inactive event can be deleted")
        status, body = request("GET", "/api/admin/events", headers=ADMIN)
        check([e["event_uuid"] for e in body["events"]]
              == [created["event_uuid"]],
              "deleted event disappears from the listing")
        status, _ = request("DELETE",
                            f"/api/admin/events/{second['event_uuid']}",
                            headers=ADMIN)
        check(status == 404, "deleting an already-deleted event is a 404")
        status, event = request("GET", "/api/event")
        check(status == 200 and event["event_uuid"] == created["event_uuid"],
              "active event is untouched by the deletion")

        print(f"\nPASS — {checks} checks")
        passed = True
    finally:
        stop_server(proc)
        if passed:
            cleanup(data_dir)
        else:
            print(f"keeping scratch dir for debugging: {data_dir}")


if __name__ == "__main__":
    main()
