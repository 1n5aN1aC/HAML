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

Log interchange follows the same no-dependency rule: ADIF export and import are both
hand-written (`client/src/adif-export.js`, `client/src/adif.js`) and run entirely in the
client against its local Dexie copy, so the server grows neither an endpoint nor a library
for them (see [CLIENT.md](../CLIENT.md), *Log interchange*). Contest-submission export
(Cabrillo, or ADIF shaped to a contest's rules) is still deferred; Templates reserve an
optional export-mapping slot for it, unused so far.

## Considered options

- FastAPI + uvicorn — better DX and free validation/OpenAPI, rejected for its larger
  dependency tree (pydantic, starlette, uvicorn).
- Stdlib-only HTTP + `websockets` package — most minimal on paper, most hand-rolled
  plumbing in practice.
- TypeScript — vetoed by the project owner; plain JS keeps ceremony low.
