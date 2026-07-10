"""WebSocket layer: presence, chat, and pokes (ADR-0005 — signals only).

Contact data never travels here. Presence lives purely in server memory and
liveness is heartbeat recency, never socket state: a station drops off the
roster when its heartbeats stop, whatever its socket is doing.

Message types:
  client -> server:  presence, chat
  server -> client:  event, presence_list, chat, poke
"""
import json
import time

from aiohttp import WSMsgType, web

import db

PRESENCE_TTL = 120      # seconds without a heartbeat before a station drops off

PRESENCE_KEYS = ("client_uuid", "callsign", "initials", "band", "mode")
CHAT_KEYS = ("uuid", "operator_callsign", "operator_initials",
             "client_uuid", "text")


def setup(app):
    app["ws_clients"] = set()
    app["presence"] = {}  # client_uuid -> {identity..., last_seen_at (epoch s)}

    async def poke():
        await broadcast(app, {"type": "poke"})

    async def notify_event():
        await broadcast(app, event_message(app))

    app["poke"] = poke
    app["notify_event"] = notify_event
    app.router.add_get("/ws", ws_handler)


async def broadcast(app, message):
    text = json.dumps(message)
    for ws in list(app["ws_clients"]):
        try:
            await ws.send_str(text)
        except (ConnectionError, RuntimeError):
            app["ws_clients"].discard(ws)


def event_message(app):
    active = app.get("event")
    return {"type": "event",
            "event_uuid": active["event_uuid"] if active else None}


def roster_message(app):
    stations = [
        {**{k: entry[k] for k in PRESENCE_KEYS},
         "last_seen_at": entry["last_seen_at"]}
        for entry in app["presence"].values()
    ]
    stations.sort(key=lambda s: s["callsign"])
    return {"type": "presence_list", "stations": stations}


def purge_stale(app):
    now = time.time()
    stale = [uuid for uuid, entry in app["presence"].items()
             if now - entry["last_seen_at"] > PRESENCE_TTL]
    for uuid in stale:
        del app["presence"][uuid]
    return bool(stale)


async def ws_handler(request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    app["ws_clients"].add(ws)
    try:
        await ws.send_str(json.dumps(event_message(app)))
        await ws.send_str(json.dumps(roster_message(app)))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "presence":
                await handle_presence(app, data)
            elif kind == "chat":
                await handle_chat(app, data)
    finally:
        app["ws_clients"].discard(ws)
        # No presence removal here: liveness is heartbeat recency, and a
        # reconnecting client shouldn't flicker off the roster.
    return ws


def valid_strings(data, keys):
    return all(isinstance(data.get(k), str) and data[k].strip() for k in keys)


async def handle_presence(app, data):
    if not valid_strings(data, PRESENCE_KEYS):
        return
    app["presence"][data["client_uuid"].strip()] = {
        **{k: data[k].strip() for k in PRESENCE_KEYS},
        "last_seen_at": time.time(),
    }
    purge_stale(app)
    await broadcast(app, roster_message(app))


async def handle_chat(app, data):
    conn = app.get("conn")
    if conn is None or not valid_strings(data, CHAT_KEYS):
        return
    stored = db.insert_chat(conn, {k: data[k].strip() if k != "text"
                                   else data[k] for k in CHAT_KEYS})
    await broadcast(app, {"type": "chat", "message": stored})



