# Tech stack: aiohttp + stdlib sqlite3 server; Vite + JavaScript + Dexie client

The server is Python with a single third-party dependency: **aiohttp**, which covers all
three server needs — async HTTP (REST API), native WebSockets (presence/chat/pokes), and
static file serving (the built client, eventually). Persistence is stdlib **sqlite3**,
one database file per Event (ADR-0002). Validation is hand-written; the API surface is
small enough that a framework doesn't pay its way. "Minimal libraries" is a real
constraint here: this runs on a club laptop dusted off once a year, and a small
dependency tree ages better.

The client is React in **plain JavaScript** (no TypeScript), built with **Vite** — whose
dev server proxies API and WebSocket traffic to aiohttp during development, and whose
production build emits static files for aiohttp to serve. IndexedDB access goes through
**Dexie**; state is plain React context/hooks, no state-management framework.

Log export (Cabrillo/ADIF) is designed-for but deferred: Templates reserve an optional
export-mapping slot (empty in v1) so stored data stays export-capable; the export
endpoint itself is post-v1.

## Considered options

- FastAPI + uvicorn — better DX and free validation/OpenAPI, rejected for its larger
  dependency tree (pydantic, starlette, uvicorn).
- Stdlib-only HTTP + `websockets` package — most minimal on paper, most hand-rolled
  plumbing in practice.
- TypeScript — vetoed by the project owner; plain JS keeps ceremony low.
