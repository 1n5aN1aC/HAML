# HAML — Architecture

HAML (HAM Logger) is a client-server web application for logging amateur radio contacts
during an event (Field Day, POTA, …) — the web-based, modern successor to the N3FJP
workflow. A Python server on a club laptop serves a React client to operator positions
over a trusted LAN.

Terminology: [GLOSSARY.md](./GLOSSARY.md). Decisions: [adr/](./adr/).
Details: [SERVER.md](./SERVER.md), [CLIENT.md](./CLIENT.md).

## The pieces

- **Server** — Python + aiohttp (sole dependency), stdlib sqlite3. Holds the authoritative
  log, answers callsign lookups, relays real-time signals, and serves the built client in
  production. One database file per Event; exactly one Event runs at a time.
  [ADR-0002](./adr/0002-one-event-per-database.md),
  [ADR-0006](./adr/0006-tech-stack.md)
- **Client** — React in plain JavaScript, built with Vite. Offline-first: contacts live in
  IndexedDB (via Dexie) and sync in the background, so a logging position keeps working
  through a network drop.
- **Trust model** — no authentication; the LAN is the boundary. Anyone can edit or delete
  any Contact; accountability comes from stamping, not permissions. The Admin page is
  gated by a shared password — a tripwire, not security.
  [ADR-0004](./adr/0004-no-auth-trusted-lan.md)

## How client and server talk

**REST carries data**: event configuration, the contact push and pull, chat history,
callsign lookups, and every admin action.

**One WebSocket per client carries signals**: presence heartbeats, chat messages, and
"pokes" that say contacts changed. The socket is **pure optimization** — with it down,
pulls continue on their timer, pending pushes keep retrying, presence goes stale, and chat
pauses. Contact sync deliberately does not run over it: that would make the socket
load-bearing and duplicate the retry and acknowledgment machinery REST already gives us.
Presence is likewise heartbeat-based rather than connection-based, and lives in server
memory only — nobody queries historical presence, and a restart would leave stale rows.

The data loop is offline-first and converges by repetition rather than by protocol
([ADR-0001](./adr/0001-offline-first-sync-model.md)):

1. The client writes new and edited Contacts to IndexedDB as `pending`.
2. **Push** — an idempotent POST upsert keyed on the client-generated UUID, retried every
   ~10s while anything is pending. Duplicate sends are harmless.
3. **Pull** — a GET for everything changed since the client's sync cursor, every ~30s and
   immediately on a poke.
4. **Pull is the acknowledgment** — a Contact becomes `synced` only when the server echoes
   it back, which makes the loop self-healing with no extra ack protocol.

Conflicts resolve last-write-wins on the server only. Deletes are soft — a flag, never a
removed row. The sync cursor is always a **server-time** timestamp; client clocks are never
trusted for sync.

**Event identity gates all of it.** The client compares its Event UUID against the server's
at boot and on every `event` message. A mismatch means the server loaded a different Event,
and the client stops and makes the operator choose rather than mixing or wiping logs on its
own ([ADR-0002](./adr/0002-one-event-per-database.md)).

**Callsign lookup** is its own REST endpoint, answered by the server from local datasets and
a cache that outlives Events. The record shape is the contract; the sources behind it are
not — see [SERVER.md](./SERVER.md).

**Log interchange runs client-side.** ADIF export and import both operate on the browser's
own replica of the log; the server has no ADIF code and no export or import endpoint — see
[CLIENT.md](./CLIENT.md).
