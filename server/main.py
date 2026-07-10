"""HAML server entry point.

Usage: python server/main.py [config.json]

Serves the REST API, the WebSocket signal layer (presence/chat/pokes), and
the built client from client/dist when it exists (during development the
Vite dev server proxies to us instead).
"""
import sys
from pathlib import Path

from aiohttp import web

import api_rest
import api_ws
import events
from config import load_config

CLIENT_DIST = Path(__file__).resolve().parent.parent / "client" / "dist"


def build_app(cfg):
    app = web.Application()
    app["cfg"] = cfg
    api_ws.setup(app)  # defines app["poke"] / app["notify_event"]
    api_rest.set_active_connection(app, events.get_active_path(cfg["data_dir"]))
    api_rest.setup_routes(app)
    if CLIENT_DIST.is_dir():
        app.router.add_get("/", lambda r: web.FileResponse(CLIENT_DIST / "index.html"))
        app.router.add_static("/", CLIENT_DIST)
    return app


def main():
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    app = build_app(cfg)
    active = app["event"]
    print(f"HAML server on {cfg['host']}:{cfg['port']} — "
          + (f"event: {active['name']}" if active else "no active event"))
    web.run_app(app, host=cfg["host"], port=cfg["port"], print=None)


if __name__ == "__main__":
    main()
