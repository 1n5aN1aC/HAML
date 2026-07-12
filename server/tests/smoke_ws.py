"""End-to-end smoke test for the WebSocket signal layer (presence, chat,
pokes, event/chat_cleared broadcasts) plus the REST endpoints tied to it
(GET /api/chat, DELETE /api/admin/chat).

Separate from smoke.py: this needs an async client that opens a socket and
asserts on server-pushed broadcasts, a different shape than smoke.py's
synchronous request/response walk of the REST API.

Requires aiohttp (already the server's only dependency) for both the WS
client and the REST calls made from this script.

Not covered here: PRESENCE_TTL expiry / purge_stale (real-time expiry is
too slow for a smoke test), reconnect storms, many concurrent clients.

Run: python server/tests/smoke_ws.py   (uses sys.executable for the subprocess)
"""
import asyncio
import json
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


def wait_for_server():
    for _ in range(50):
        time.sleep(0.1)
        try:
            urllib.request.urlopen(BASE + "/api/event", timeout=1)
            return
        except urllib.error.HTTPError:
            return  # 404 (no active event) still means the server is up
        except (urllib.error.URLError, ConnectionError):
            continue
    raise AssertionError("server never came up")


async def rest(session, method, path, body=None, headers=None):
    """Returns (status, parsed json)."""
    async with session.request(method, BASE + path, json=body,
                               headers=headers or {}) as resp:
        return resp.status, json.loads(await resp.text())


async def next_message(ws, timeout=2):
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    return json.loads(msg.data)


async def no_more_messages(ws, timeout=0.3):
    """True if no message arrives within timeout — used to prove a
    malformed message was silently ignored rather than broadcast."""
    try:
        await asyncio.wait_for(ws.receive(), timeout=timeout)
        return False
    except asyncio.TimeoutError:
        return True


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

        print("malformed chat is ignored:")
        bad_chat = chat("client-A", "should not land", msg_uuid="chat-2")
        del bad_chat["operator_callsign"]
        await client_a.send_str(json.dumps(bad_chat))
        check(await no_more_messages(client_a),
              "chat missing operator_callsign produces no broadcast")
        status, body = await rest(session, "GET", "/api/chat")
        check(len(body["messages"]) == 1,
              "chat history unchanged after the malformed send")

        print("poke on contact write:")
        contact = {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "qso_at": "2026-01-01T00:00:00.000Z",
            "last_edited": "2026-01-01T00:00:00.000Z",
            "remote_callsign": "N0CALL", "operator_callsign": "W7XYZ",
            "operator_initials": "JD", "client_uuid": "client-A",
            "band": "20m", "mode": "Phone", "deleted": False,
            "fields": {"class": "3A", "section": "OR"},
        }
        status, _ = await rest(session, "POST", "/api/contacts", body=contact)
        check(status == 200, "contact stored")
        poke_a = await next_message(client_a)
        poke_b = await next_message(client_b)
        check(poke_a == {"type": "poke"} == poke_b,
              "both clients are poked after a contact write")

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
        check(body["messages"] == [], "REST chat history is empty after clearing")

        print("disconnect doesn't flicker the roster:")
        await client_b.close()
        client_c = await session.ws_connect(WS_URL)
        await next_message(client_c)  # event
        snapshot = await next_message(client_c)
        check("client-B" in {s["client_uuid"] for s in snapshot["stations"]},
              "closed client's station survives in a fresh snapshot")

        await client_a.close()
        await client_c.close()

    print(f"\nPASS — {checks} checks")


if __name__ == "__main__":
    data_dir = Path(tempfile.mkdtemp(prefix="haml-smoke-ws-"))
    config_path = data_dir / "config.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": PORT,
        "data_dir": str(data_dir), "admin_password": "test-pw",
    }))
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "main.py"),
                             str(config_path)])
    try:
        wait_for_server()
        asyncio.run(main())
    finally:
        proc.terminate()
        proc.wait(timeout=10)
