"""End-to-end smoke test for the WebSocket signal layer (presence heartbeats
and in-place roster updates, chat — including verbatim text handling and
blank-text rejection — pokes, event/chat_cleared broadcasts,
event-switch broadcasts, and garbage-frame handling) plus the REST endpoints
tied to it (GET /api/chat, DELETE /api/admin/chat). Also covers chat scoping
across event switches and chat persistence across a server restart —
presence, by contrast, is memory-only and empties on restart.

Separate from smoke.py: this needs an async client that opens a socket and
asserts on server-pushed broadcasts, a different shape than smoke.py's
synchronous request/response walk of the REST API.

Requires aiohttp (already the server's only dependency) for both the WS
client and the REST calls made from this script.

Not covered here: PRESENCE_TTL expiry / purge_stale (real-time expiry is
too slow for a smoke test), the dead-socket discard path in broadcast()
(not deterministically reachable from outside the process), reconnect
storms, many concurrent clients, and the on_shutdown close_all handler
(GOING_AWAY close on open sockets — this script tears down with
proc.terminate(), a hard kill on Windows that never runs aiohttp's
shutdown hooks).

Run: python server/tests/smoke_ws.py   (uses sys.executable for the subprocess)
"""
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

SERVER_DIR = Path(__file__).resolve().parent.parent
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/ws"
ADMIN = {"X-Admin-Password": "test-pw"}

checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ok: {label}")


def wait_for_server(proc):
    for _ in range(50):
        time.sleep(0.1)
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited early (code {proc.returncode}) — "
                f"is port {PORT} already in use?")
        try:
            urllib.request.urlopen(BASE + "/api/event", timeout=1)
            return
        except urllib.error.HTTPError:
            return  # 404 (no active event) still means the server is up
        except (urllib.error.URLError, ConnectionError):
            continue
    raise AssertionError("server never came up")


def start_server(config_path):
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "main.py"),
                             str(config_path)])
    try:
        wait_for_server(proc)
    except Exception:
        stop_server(proc)
        raise
    return proc


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def cleanup(data_dir):
    """Remove the scratch dir, retrying briefly: on Windows the server's db
    file handles can outlive proc.wait() by a moment."""
    for _ in range(10):
        shutil.rmtree(data_dir, ignore_errors=True)
        if not data_dir.exists():
            return
        time.sleep(0.2)
    print(f"warning: could not remove {data_dir}")


async def rest(session, method, path, body=None, headers=None):
    """Returns (status, parsed json), failing informatively on a non-JSON
    body (e.g. the plain-text 404 aiohttp serves for an unmatched route)."""
    async with session.request(method, BASE + path, json=body,
                               headers=headers or {}) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except json.JSONDecodeError:
            raise AssertionError(
                f"FAIL: {method} {path} returned {resp.status} "
                f"with a non-JSON body: {text[:200]!r}") from None


async def next_message(ws, timeout=2):
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if msg.type != aiohttp.WSMsgType.TEXT:
        raise AssertionError(
            f"expected a TEXT frame, got {msg.type.name}: {msg.data!r}")
    return json.loads(msg.data)


async def no_more_messages(ws, timeout=2):
    """True if no message arrives within timeout — used to prove a
    malformed message was silently ignored rather than broadcast. The
    window is generous so a slow machine can't false-pass by delaying
    a broadcast past it (at the cost of a slower run)."""
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    except asyncio.TimeoutError:
        return True
    if msg.type != aiohttp.WSMsgType.TEXT:
        raise AssertionError(
            f"socket closed/errored while expecting silence: "
            f"{msg.type.name}: {msg.data!r}")
    print(f"  unexpected broadcast: {msg.data}")
    return False


def presence(client_uuid, callsign, initials="JD", band="20m", mode="Phone"):
    return {"type": "presence", "client_uuid": client_uuid,
            "callsign": callsign, "initials": initials,
            "band": band, "mode": mode}


def chat(client_uuid, text, callsign="KJ7ABC", initials="JD", msg_uuid="chat-1"):
    return {"type": "chat", "uuid": msg_uuid, "operator_callsign": callsign,
            "operator_initials": initials, "client_uuid": client_uuid,
            "text": text}


async def main():
    async with aiohttp.ClientSession() as session:
        print("boot + connect before any event exists:")
        client_a = await session.ws_connect(WS_URL)
        first = await next_message(client_a)
        check(first == {"type": "event", "event_uuid": None},
              "initial 'event' message has no active event")
        second = await next_message(client_a)
        check(second == {"type": "presence_list", "stations": []},
              "initial 'presence_list' message is empty")

        print("chat with no active event is dropped:")
        await client_a.send_str(json.dumps(chat("client-A", "hello early")))
        check(await no_more_messages(client_a),
              "chat before any event exists produces no broadcast")

        print("presence with no active event still works:")
        # presence needs no db connection; same client_uuid as the later
        # sections, so the global in-memory roster stays a single station
        await client_a.send_str(json.dumps(presence("client-A", "W7XYZ")))
        roster = await next_message(client_a)
        check(roster["type"] == "presence_list"
              and {s["client_uuid"] for s in roster["stations"]} == {"client-A"},
              "heartbeat before any event exists still broadcasts the roster")

        print("create event:")
        status, created = await rest(session, "POST", "/api/admin/events",
                                     headers=ADMIN,
                                     body={"template": "field-day",
                                           "name": "WS Smoke",
                                           "station_callsign": "W7XYZ"})
        check(status == 201, "event created")
        ev_msg = await next_message(client_a)
        check(ev_msg == {"type": "event", "event_uuid": created["event_uuid"]},
              "connected client is notified of the new event")

        print("presence heartbeat:")
        await client_a.send_str(json.dumps(presence("client-A", "W7XYZ")))
        roster = await next_message(client_a)
        check(roster["type"] == "presence_list"
              and {s["client_uuid"] for s in roster["stations"]} == {"client-A"},
              "roster reflects a single valid heartbeat")

        print("presence update (same client, new identity):")
        await client_a.send_str(json.dumps(presence(
            " client-A ", "  W7XYZ  ", initials=" AB ",
            band=" 40m ", mode=" CW ")))
        roster = await next_message(client_a)
        check(len(roster["stations"]) == 1,
              "repeat heartbeat updates the entry in place, no duplicate")
        station = roster["stations"][0]
        check({k: station[k] for k in ("client_uuid", "callsign", "initials",
                                       "band", "mode")}
              == {"client_uuid": "client-A", "callsign": "W7XYZ",
                  "initials": "AB", "band": "40m", "mode": "CW"},
              "updated identity lands with whitespace stripped")
        check(isinstance(station["last_seen_at"], (int, float))
              and abs(time.time() - station["last_seen_at"]) < 10,
              "roster entry carries a fresh last_seen_at")

        print("malformed presence is ignored:")
        bad = presence("client-A", "W7XYZ")
        del bad["band"]
        await client_a.send_str(json.dumps(bad))
        check(await no_more_messages(client_a),
              "presence missing a required key produces no broadcast")
        blank = presence("client-A", "  ")
        await client_a.send_str(json.dumps(blank))
        check(await no_more_messages(client_a),
              "presence with a blank field produces no broadcast")
        await client_a.send_str(json.dumps(presence("client-A", "W7XYZ")))
        roster = await next_message(client_a)
        check(len(roster["stations"]) == 1,
              "roster still has exactly one station after the malformed sends")

        print("garbage frames are ignored:")
        # batch the sends behind a single silence window: any erroneous
        # broadcast from an earlier send would still arrive within it
        await client_a.send_str("definitely not json")
        await client_a.send_str(json.dumps({"type": "bogus"}))
        await client_a.send_str(json.dumps({"hello": "no type"}))
        await client_a.send_bytes(b"\x00\x01binary")
        check(await no_more_messages(client_a),
              "non-JSON, unknown-type, untyped, and binary frames"
              " produce no broadcast")
        await client_a.send_str(json.dumps(presence("client-A", "W7XYZ")))
        roster = await next_message(client_a)
        check(roster["type"] == "presence_list",
              "socket and dispatcher survive the garbage frames")

        print("second client:")
        client_b = await session.ws_connect(WS_URL)
        await next_message(client_b)  # event
        await next_message(client_b)  # presence_list snapshot
        await client_b.send_str(json.dumps(presence("client-B", "KJ7ABC")))
        roster_a = await next_message(client_a)
        roster_b = await next_message(client_b)
        for roster in (roster_a, roster_b):
            check({s["client_uuid"] for s in roster["stations"]}
                  == {"client-A", "client-B"},
                  "both clients see both stations (order not asserted)")

        print("chat round trip:")
        await client_a.send_str(json.dumps(chat("client-A", "CQ CQ")))
        chat_a = await next_message(client_a)
        chat_b = await next_message(client_b)
        for msg in (chat_a, chat_b):
            check(msg["type"] == "chat" and msg["message"]["text"] == "CQ CQ",
                  "both clients receive the chat broadcast")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200 and len(body["messages"]) == 1
              and body["messages"][0]["text"] == "CQ CQ",
              "REST chat history shows the message sent over WS")

        print("duplicate chat uuid is idempotent:")
        # a resend (e.g. after a reconnect) re-broadcasts the stored row,
        # so the original text wins even if the resend's text differs
        await client_a.send_str(json.dumps(
            chat("client-A", "EDITED TEXT", msg_uuid="chat-1")))
        dup_a = await next_message(client_a)
        dup_b = await next_message(client_b)
        for msg in (dup_a, dup_b):
            check(msg["type"] == "chat" and msg["message"]["text"] == "CQ CQ",
                  "duplicate uuid re-broadcasts the original message")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200 and len(body["messages"]) == 1
              and body["messages"][0]["text"] == "CQ CQ",
              "chat history is unchanged after the duplicate send")

        print("malformed chat is ignored:")
        bad_chat = chat("client-A", "should not land", msg_uuid="chat-2")
        del bad_chat["operator_callsign"]
        await client_a.send_str(json.dumps(bad_chat))
        check(await no_more_messages(client_a),
              "chat missing operator_callsign produces no broadcast")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200 and len(body["messages"]) == 1,
              "chat history unchanged after the malformed send")

        print("chat text handling:")
        # text is kept verbatim (whitespace and all); identity keys are stripped
        await client_a.send_str(json.dumps(
            chat("client-A", "  padded text  ", callsign=" KJ7ABC ",
                 msg_uuid="chat-3")))
        pad_a = await next_message(client_a)
        pad_b = await next_message(client_b)
        for msg in (pad_a, pad_b):
            check(msg["type"] == "chat"
                  and msg["message"]["text"] == "  padded text  "
                  and msg["message"]["operator_callsign"] == "KJ7ABC",
                  "broadcast keeps text verbatim but strips identity fields")
        status, body = await rest(session, "GET", "/api/chat")
        stored = next((m for m in body["messages"] if m["uuid"] == "chat-3"),
                      None)
        check(status == 200 and len(body["messages"]) == 2
              and stored is not None and stored["text"] == "  padded text  "
              and stored["operator_callsign"] == "KJ7ABC",
              "stored row keeps text verbatim but strips identity fields")
        await client_a.send_str(json.dumps(
            chat("client-A", "   ", msg_uuid="chat-4")))
        check(await no_more_messages(client_a),
              "whitespace-only chat text produces no broadcast")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200 and len(body["messages"]) == 2,
              "chat history unchanged after the blank-text send")

        print("poke on contact write:")
        contact = {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "qso_at": "2026-01-01T00:00:00.000Z",
            "last_edited": "2026-01-01T00:00:00.000Z",
            "remote_callsign": "N0CALL", "operator_callsign": "W7XYZ",
            "operator_initials": "JD", "client_uuid": "client-A",
            "band": "20m", "mode": "Phone", "deleted": False,
            "section": "OR",  # built-in: top-level column, not a blob key
            "fields": {"class": "3A"},
        }
        status, _ = await rest(session, "POST", "/api/contacts", body=contact)
        check(status == 200, "contact stored")
        poke_a = await next_message(client_a)
        poke_b = await next_message(client_b)
        check(poke_a == {"type": "poke"} == poke_b,
              "both clients are poked after a contact write")

        print("no poke on a losing LWW write:")
        stale = dict(contact, operator_initials="ZZ",
                     last_edited="2025-12-31T00:00:00.000Z")
        status, body = await rest(session, "POST", "/api/contacts", body=stale)
        check(status == 200 and body["stored"] is False,
              "older edit is rejected (LWW)")
        check(await no_more_messages(client_a),
              "losing write produces no poke")

        print("chat clear (REST, admin-gated):")
        status, _ = await rest(session, "DELETE", "/api/admin/chat")
        check(status == 401, "chat clear rejects a missing password")
        status, body = await rest(session, "DELETE", "/api/admin/chat",
                                  headers=ADMIN)
        check(status == 200 and body["cleared"] is True, "chat cleared")
        cleared_a = await next_message(client_a)
        cleared_b = await next_message(client_b)
        check(cleared_a == {"type": "chat_cleared"} == cleared_b,
              "both clients receive chat_cleared")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200 and body["messages"] == [],
              "REST chat history is empty after clearing")

        print("event switch broadcast + chat follows the active event:")
        await client_a.send_str(json.dumps(
            chat("client-A", "survives the switch", msg_uuid="chat-persist")))
        await next_message(client_a)  # chat broadcast
        await next_message(client_b)  # chat broadcast
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200
              and [m["text"] for m in body["messages"]]
              == ["survives the switch"],
              "first event's chat history holds the pre-switch message")
        status, second = await rest(session, "POST", "/api/admin/events",
                                    headers=ADMIN,
                                    body={"template": "pota",
                                          "name": "WS Smoke 2",
                                          "station_callsign": "KJ7ABC"})
        check(status == 201, "second event created")
        ev_a = await next_message(client_a)
        ev_b = await next_message(client_b)
        check(ev_a == {"type": "event",
                       "event_uuid": second["event_uuid"]} == ev_b,
              "both clients are notified when a new event is created")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200 and body["messages"] == [],
              "new event starts with an empty chat")
        await client_a.send_str(json.dumps(
            chat("client-A", "only in event 2", msg_uuid="chat-e2")))
        await next_message(client_a)  # chat broadcast
        await next_message(client_b)  # chat broadcast
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200
              and [m["text"] for m in body["messages"]] == ["only in event 2"],
              "chat sent in the new event lands in its own history")
        status, _ = await rest(
            session, "POST",
            f"/api/admin/events/{created['event_uuid']}/activate",
            headers=ADMIN, body={})
        check(status == 200, "first event re-activated")
        ev_a = await next_message(client_a)
        ev_b = await next_message(client_b)
        check(ev_a == {"type": "event",
                       "event_uuid": created["event_uuid"]} == ev_b,
              "both clients are notified when an existing event is activated")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200
              and [m["text"] for m in body["messages"]]
              == ["survives the switch"],
              "chat history follows the active event back")

        print("disconnect doesn't flicker the roster:")
        await client_b.close()
        client_c = await session.ws_connect(WS_URL)
        await next_message(client_c)  # event
        snapshot = await next_message(client_c)
        check("client-B" in {s["client_uuid"] for s in snapshot["stations"]},
              "closed client's station survives in a fresh snapshot")

        await client_a.close()
        await client_c.close()

    return created["event_uuid"]


async def post_restart(event_uuid):
    """After a server restart: chat (in the event db) persists, presence
    (in server memory) is gone."""
    async with aiohttp.ClientSession() as session:
        print("restart persistence:")
        status, body = await rest(session, "GET", "/api/chat")
        check(status == 200
              and [m["text"] for m in body["messages"]]
              == ["survives the switch"],
              "chat history survives a server restart")
        ws = await session.ws_connect(WS_URL)
        first = await next_message(ws)
        check(first == {"type": "event", "event_uuid": event_uuid},
              "restarted server announces the same active event")
        snapshot = await next_message(ws)
        check(snapshot == {"type": "presence_list", "stations": []},
              "presence roster is empty after a restart (memory-only)")
        await ws.close()


if __name__ == "__main__":
    data_dir = Path(tempfile.mkdtemp(prefix="haml-smoke-ws-"))
    config_path = data_dir / "config.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": PORT,
        "data_dir": str(data_dir), "admin_password": "test-pw",
    }))
    proc = start_server(config_path)
    passed = False
    try:
        event_uuid = asyncio.run(main())
        stop_server(proc)
        proc = start_server(config_path)
        asyncio.run(post_restart(event_uuid))
        print(f"\nPASS — {checks} checks")
        passed = True
    finally:
        stop_server(proc)
        if passed:
            cleanup(data_dir)
        else:
            print(f"keeping scratch dir for debugging: {data_dir}")
