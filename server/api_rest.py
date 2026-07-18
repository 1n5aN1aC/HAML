# REST API: data endpoints (ADR-0005 — REST carries data).
#
# Contacts: one record per request, idempotent LWW upsert (ADR-0001).
# Admin endpoints are gated by the shared password header (ADR-0004).
#
import asyncio
import json
import math
import sqlite3

from aiohttp import web

import db
import events
import lookup
import lookup_cache
import templates


# Build a JSON error response with the given status and message.
def json_error(status, message):
    return web.json_response({"error": message}, status=status)

# The active Event's DB connection, or None when no Event is loaded.
def get_conn(request):
    return request.app.get("conn")

# Return the active Event's DB connection, or raise 404 if none.
def require_event(request):
    conn = get_conn(request)
    if conn is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "no active event"}),
            content_type="application/json",
        )
    return conn

# Names of required fields that are absent or blank on a contact body.
# Mirrors the client's resolution (builtin-fields.js resolveField): a field's
# 'required' flag is enforced only when the field is opted into the entry
# box (a field hidden from entry can never be filled at log time). Built-in
# values live top-level on the body; custom values live in the 'fields' blob.
def missing_required_fields(config, body):
    fields = body.get("fields") or {}
    missing = []
    for f in config.get("fields") or []:
        if not (f.get("entry") and f.get("required")):
            continue
        name = f["name"]
        value = body.get(name) if name in db.BUILTIN_FIELDS else fields.get(name)
        if not str(value or "").strip():
            missing.append(name)
    return missing

# Check the X-Admin-Password header against the configured password, raising 401 if it doesn't match.
def require_admin(request):
    password = request.app["cfg"]["admin_password"]
    if request.headers.get("X-Admin-Password") != password:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "bad admin password"}),
            content_type="application/json",
        )

# Close any current Event connection and open the one at db_path.
# A db_path that isn't a readable Event database (e.g. state.json pointing at
# a corrupt file at boot) falls back to no active event rather than crashing.
def set_active_connection(app, db_path):
    old = app.get("conn")
    if old is not None:
        old.close()
    if db_path is None:
        app["conn"] = None
        app["event"] = None
        return
    try:
        conn = db.open_db(db_path)
        event = {
            "event_uuid": db.meta_get(conn, "event_uuid"),
            "name": db.meta_get(conn, "event_name"),
            "station_callsign": db.meta_get(conn, "station_callsign"),
            "local_exchange": db.meta_get(conn, "local_exchange"),
            "config": json.loads(db.meta_get(conn, "config", "{}")),
        }
    except sqlite3.Error as exc:
        print(f"warning: cannot open active event db {db_path}: {exc} — "
              "starting with no active event")
        app["conn"] = None
        app["event"] = None
        return
    app["conn"] = conn
    app["event"] = event


# --- data endpoints ---------------------------------------------------------

# Return the active event's metadata plus the current server time.
async def get_event(request):
    require_event(request)
    return web.json_response(dict(request.app["event"], server_time=db.now_iso()))


# Upsert a contact record.
# Validates, upserts, and notify listeners of the change.
async def post_contact(request):
    conn = require_event(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error(400, "body must be JSON")
    try:
        contact = db.validate_contact(body)
    except ValueError as exc:
        return json_error(400, str(exc))
    # tombstones are exempt: a deletion must always be able to sync
    if not contact["deleted"]:
        missing = missing_required_fields(request.app["event"]["config"], body)
        if missing:
            return json_error(
                400, "missing required fields: " + ", ".join(missing))
    stored = db.upsert_contact(conn, contact)
    if stored:
        await request.app["poke"]()
    return web.json_response({"stored": stored, "server_time": db.now_iso()})


# Return contacts changed since the optional 'since' query timestamp.
async def get_contacts(request):
    conn = require_event(request)
    since = request.query.get("since")
    try:
        contacts = db.contacts_since(conn, since)
    except ValueError:
        return json_error(400, "bad 'since' timestamp")
    return web.json_response({"contacts": contacts, "server_time": db.now_iso()})


# Return the full chat history.
async def get_chat(request):
    conn = require_event(request)
    return web.json_response(
        {"messages": db.chat_history(conn), "server_time": db.now_iso()}
    )


# --- callsign lookup --------------------------------------------------------
# Lookup is a separate concern from the active Event (Q6: the cache lives
# in its own file and outlives events). The handler is therefore not gated
# by require_event() — it works even when no Event is loaded, so the Admin
# page can still use it for diagnosing setup.

LONGPOLL_TIMEOUT_S = 15

# Mean Earth radius in kilometers.
_EARTH_RADIUS_KM = 6371.0


def _with_distance(app, record):
    """Return the record plus a `distance` key: Haversine kilometers, rounded down to a whole number
    From the active event's operating position (config.location) to the record's coordinates.
    null when missing.

    Distance is event-relative, so it is stamped on the response here and
    never stored in the canonical record or the cache — a cached row must
    stay correct when a different event becomes active.
    """
    event = app.get("event") or {}
    loc = (event.get("config") or {}).get("location")
    lat, lon = record.get("latitude"), record.get("longitude")
    distance = None
    if loc and lat is not None and lon is not None:
        phi1 = math.radians(loc["latitude"])
        phi2 = math.radians(lat)
        d_phi = math.radians(lat - loc["latitude"])
        d_lam = math.radians(lon - loc["longitude"])
        a = (math.sin(d_phi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        distance = math.floor(_EARTH_RADIUS_KM * c)
    return dict(record, distance=distance)


async def post_lookup(request):
    """Look up a callsign, returning the cached or fresh result.
    The 200 response body is the canonical record from `lookup_record`, plus
    a request-time `distance` field (see _with_distance):
    The client can trust its field names, types, and value sets without validating.

    Primary provider is the local FCC ULS sqlite — instant, offline, one indexed query per call.
    On an FCC miss or error the chain falls through to the CallParser prefix DB
    (see lookup._run_lookup), which answers with DXCC-level fields only —
    source "callparser", never cached.
    The cache layer + long-poll handler shape is kept for a future online fallback (QRZ/HamQTH for non-US calls);
    today the cache read path never hits and the long-poll ceiling only matters if a future provider is wired in.

    Cache-first, then long-poll for misses:
      - cache hit ok         -> 200 + canonical record, instant
      - cache hit not_found  -> 404 (no row, no upstream)
      - cache hit error      -> 502 (transient upstream failure)
      - cache miss           -> coalesce, run upstream, wait up to 15s
      - timeout (no result in 15s) -> 408 (no cache write; client retries)

    Two concurrent POSTs for the same callsign share a single upstream hit.

    TTLs (see lookup_cache, reserved for future online providers):
      - ok, clean   -> 365 days
      - ok, dirty   -> 15 min  (one or more fields failed coercion)
      - not_found   -> 30 days
      - error       -> 15 min
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error(400, "body must be JSON")
    raw = body.get("callsign") if isinstance(body, dict) else None
    callsign = lookup.normalize_callsign(raw or "")
    if not callsign:
        return json_error(400, "callsign must be a non-empty string")

    cache_conn = request.app["lookup_cache"]
    cached = lookup_cache.get(cache_conn, callsign)
    if cached is not None:
        if cached["status"] == lookup_cache.STATUS_OK:
            return web.json_response(
                _with_distance(request.app, json.loads(cached["payload"])))
        if cached["status"] == lookup_cache.STATUS_NOT_FOUND:
            return json_error(404, "callsign not found")
        # status == error
        return json_error(502, cached["error"] or "upstream lookup failed")

    # Cache miss: coalesce, schedule, await under the long-poll ceiling.
    future = lookup.schedule(request.app, callsign)
    # asyncio.shield keeps wait_for() from cancelling the shared future on timeout:
    # the _drive task keeps running, writes a cache row, and concurrent/late clients still get the result instead of a CancelledError.
    try:
        result = await asyncio.wait_for(asyncio.shield(future),
                                        timeout=LONGPOLL_TIMEOUT_S)
    except asyncio.TimeoutError:
        return json_error(408, "lookup timed out")
    if result["status"] == lookup_cache.STATUS_OK:
        return web.json_response(_with_distance(request.app, result["payload"]))
    if result["status"] == lookup_cache.STATUS_NOT_FOUND:
        return json_error(404, "callsign not found")
    return json_error(502, result["error"] or "upstream lookup failed")


# --- admin endpoints --------------------------------------------------------

# List available event templates loaded from disk.
async def admin_list_templates(request):
    require_admin(request)
    return web.json_response({"templates": templates.list_templates()})


# Return one template's full JSON, for the admin template editor.
async def admin_get_template(request):
    require_admin(request)
    try:
        template = templates.load_template(request.match_info["template_id"])
    except (ValueError, json.JSONDecodeError) as exc:
        return json_error(404, str(exc))
    return web.json_response(template)


# Create or overwrite an event template file on disk.
async def admin_save_template(request):
    require_admin(request)
    template_id = request.match_info["template_id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error(400, "body must be JSON")
    try:
        templates.save_template(template_id, body)
    except ValueError as exc:
        return json_error(400, str(exc))
    return web.json_response({"id": template_id, "name": body["name"]})


# Delete an event template file from disk.
async def admin_delete_template(request):
    require_admin(request)
    template_id = request.match_info["template_id"]
    if not templates.delete_template(template_id):
        return json_error(404, "no such template")
    return web.json_response({"deleted": template_id})


# List all events stored on disk.
async def admin_list_events(request):
    require_admin(request)
    return web.json_response(
        {"events": events.list_events(request.app["cfg"]["data_dir"])}
    )


# Create a new event from a template and activate it.
async def admin_create_event(request):
    require_admin(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error(400, "body must be JSON")
    name = body.get("name")
    station_callsign = body.get("station_callsign")
    if not isinstance(name, str) or not name.strip():
        return json_error(400, "event needs a name")
    if not isinstance(station_callsign, str) or not station_callsign.strip():
        return json_error(400, "event needs a station_callsign")
    # Optional display-only exchange shown in the client's status bar.
    local_exchange = body.get("local_exchange")
    if local_exchange is not None and not isinstance(local_exchange, str):
        return json_error(400, "local_exchange must be a string")
    local_exchange = (local_exchange or "").strip().upper() or None
    try:
        location = events.validate_location(body.get("location"))
    except ValueError as exc:
        return json_error(400, str(exc))
    try:
        template = templates.load_template(body.get("template", ""))
    except (ValueError, json.JSONDecodeError) as exc:
        return json_error(400, f"bad template: {exc}")

    data_dir = request.app["cfg"]["data_dir"]
    meta = events.create_event(data_dir, template, name.strip(),
                               station_callsign.strip().upper(), location,
                               local_exchange)
    set_active_connection(request.app, events.get_active_path(data_dir))
    await request.app["notify_event"]()
    return web.json_response(meta, status=201)


# Activate an existing event by its UUID.
async def admin_activate_event(request):
    require_admin(request)
    data_dir = request.app["cfg"]["data_dir"]
    path = events.activate_event(data_dir, request.match_info["event_uuid"])
    if path is None:
        return json_error(404, "no such event")
    set_active_connection(request.app, path)
    await request.app["notify_event"]()
    return web.json_response(request.app["event"])


# Delete an inactive event's database file from disk.
async def admin_delete_event(request):
    require_admin(request)
    data_dir = request.app["cfg"]["data_dir"]
    event_uuid = request.match_info["event_uuid"]
    try:
        if not events.delete_event(data_dir, event_uuid):
            return json_error(404, "no such event")
    except ValueError as exc:
        return json_error(400, str(exc))
    return web.json_response({"deleted": event_uuid})


# Snapshot the active event to a backup file.
async def admin_backup(request):
    require_admin(request)
    conn = require_event(request)
    path = events.backup_event(conn, request.app["cfg"]["data_dir"],
                               request.app["event"]["name"])
    return web.json_response({"backup": path.name})


# Delete all chat messages from the active event and tell clients to clear.
async def admin_clear_chat(request):
    require_admin(request)
    conn = require_event(request)
    db.clear_chat(conn)
    await request.app["notify_chat_cleared"]()
    return web.json_response({"cleared": True})


# Raw lookup-cache row counts by status (expired rows included).
async def admin_lookup_cache_stats(request):
    require_admin(request)
    return web.json_response(lookup_cache.stats(request.app["lookup_cache"]))


# Delete every row from the lookup cache.
async def admin_clear_lookup_cache(request):
    require_admin(request)
    deleted = lookup_cache.clear(request.app["lookup_cache"])
    return web.json_response({"cleared": True, "deleted": deleted})


# Register all REST routes on the given app.
def setup_routes(app):
    app.router.add_get("/api/event", get_event)
    app.router.add_post("/api/contacts", post_contact)
    app.router.add_get("/api/contacts", get_contacts)
    app.router.add_get("/api/chat", get_chat)
    app.router.add_post("/api/lookup", post_lookup)
    app.router.add_get("/api/admin/templates", admin_list_templates)
    app.router.add_get("/api/admin/templates/{template_id}",
                       admin_get_template)
    app.router.add_put("/api/admin/templates/{template_id}",
                       admin_save_template)
    app.router.add_delete("/api/admin/templates/{template_id}",
                          admin_delete_template)
    app.router.add_get("/api/admin/events", admin_list_events)
    app.router.add_post("/api/admin/events", admin_create_event)
    app.router.add_post("/api/admin/events/{event_uuid}/activate",
                        admin_activate_event)
    app.router.add_delete("/api/admin/events/{event_uuid}",
                          admin_delete_event)
    app.router.add_post("/api/admin/backup", admin_backup)
    app.router.add_delete("/api/admin/chat", admin_clear_chat)
    app.router.add_get("/api/admin/lookup-cache", admin_lookup_cache_stats)
    app.router.add_delete("/api/admin/lookup-cache", admin_clear_lookup_cache)
