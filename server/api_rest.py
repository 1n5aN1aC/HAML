"""REST API: data endpoints (ADR-0005 — REST carries data).

Contacts: one record per request, idempotent LWW upsert (ADR-0001).
Admin endpoints are gated by the shared password header (ADR-0004).
"""
import json

from aiohttp import web

import db
import events
import templates


def json_error(status, message):
    return web.json_response({"error": message}, status=status)


def get_conn(request):
    """The active Event's DB connection, or None when no Event is loaded."""
    return request.app.get("conn")


def require_event(request):
    conn = get_conn(request)
    if conn is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "no active event"}),
            content_type="application/json",
        )
    return conn


def missing_required_fields(config, fields):
    """Names of required template fields that are absent or blank."""
    return [f["name"] for f in config.get("fields", [])
            if f.get("required") and not str(fields.get(f["name"], "")).strip()]


def require_admin(request):
    password = request.app["cfg"]["admin_password"]
    if request.headers.get("X-Admin-Password") != password:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "bad admin password"}),
            content_type="application/json",
        )


def set_active_connection(app, db_path):
    """Close any current Event connection and open the one at db_path."""
    old = app.get("conn")
    if old is not None:
        old.close()
    if db_path is None:
        app["conn"] = None
        app["event"] = None
        return
    conn = db.open_db(db_path)
    app["conn"] = conn
    app["event"] = {
        "event_uuid": db.meta_get(conn, "event_uuid"),
        "name": db.meta_get(conn, "event_name"),
        "station_callsign": db.meta_get(conn, "station_callsign"),
        "config": json.loads(db.meta_get(conn, "config", "{}")),
    }


# --- data endpoints ---------------------------------------------------------

async def get_event(request):
    require_event(request)
    return web.json_response(dict(request.app["event"], server_time=db.now_iso()))


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
        missing = missing_required_fields(request.app["event"]["config"],
                                          body["fields"])
        if missing:
            return json_error(
                400, "missing required fields: " + ", ".join(missing))
    stored = db.upsert_contact(conn, contact)
    if stored:
        await request.app["poke"]()
    return web.json_response({"stored": stored, "server_time": db.now_iso()})


async def get_contacts(request):
    conn = require_event(request)
    since = request.query.get("since")
    try:
        contacts = db.contacts_since(conn, since)
    except ValueError:
        return json_error(400, "bad 'since' timestamp")
    return web.json_response({"contacts": contacts, "server_time": db.now_iso()})


async def get_chat(request):
    conn = require_event(request)
    return web.json_response(
        {"messages": db.chat_history(conn), "server_time": db.now_iso()}
    )


# --- admin endpoints --------------------------------------------------------

async def admin_list_templates(request):
    require_admin(request)
    return web.json_response({"templates": templates.list_templates()})


async def admin_list_events(request):
    require_admin(request)
    return web.json_response(
        {"events": events.list_events(request.app["cfg"]["data_dir"])}
    )


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
    try:
        template = templates.load_template(body.get("template", ""))
    except (ValueError, json.JSONDecodeError) as exc:
        return json_error(400, f"bad template: {exc}")

    data_dir = request.app["cfg"]["data_dir"]
    meta = events.create_event(data_dir, template, name.strip(),
                               station_callsign.strip().upper())
    set_active_connection(request.app, events.get_active_path(data_dir))
    await request.app["notify_event"]()
    return web.json_response(meta, status=201)


async def admin_activate_event(request):
    require_admin(request)
    data_dir = request.app["cfg"]["data_dir"]
    path = events.activate_event(data_dir, request.match_info["event_uuid"])
    if path is None:
        return json_error(404, "no such event")
    set_active_connection(request.app, path)
    await request.app["notify_event"]()
    return web.json_response(request.app["event"])


async def admin_backup(request):
    require_admin(request)
    conn = require_event(request)
    path = events.backup_event(conn, request.app["cfg"]["data_dir"],
                               request.app["event"]["name"])
    return web.json_response({"backup": path.name})


def setup_routes(app):
    app.router.add_get("/api/event", get_event)
    app.router.add_post("/api/contacts", post_contact)
    app.router.add_get("/api/contacts", get_contacts)
    app.router.add_get("/api/chat", get_chat)
    app.router.add_get("/api/admin/templates", admin_list_templates)
    app.router.add_get("/api/admin/events", admin_list_events)
    app.router.add_post("/api/admin/events", admin_create_event)
    app.router.add_post("/api/admin/events/{event_uuid}/activate",
                        admin_activate_event)
    app.router.add_post("/api/admin/backup", admin_backup)
