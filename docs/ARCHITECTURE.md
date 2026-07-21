# HAML — Architecture

HAML (HAM Logger) is a client-server web application for logging amateur radio contacts
during an event (Field Day, POTA, …) — the web-based, modern successor to the N3FJP
workflow. A Python server on a club laptop serves a React client to operator positions
over a trusted LAN.

Terminology: [GLOSSARY.md](./GLOSSARY.md).
Details: [SERVER.md](./SERVER.md), [CLIENT.md](./CLIENT.md).

## The pieces

- **Server** — Python + aiohttp, stdlib sqlite3. Holds the authoritative log, answers
  callsign lookups, relays real-time signals, and serves the built client in production.
  One database file per Event; exactly one Event runs at a time.
- **Client** — React in plain JavaScript, built with Vite. Offline-first: contacts live in
  IndexedDB (via Dexie) and sync in the background, so a logging position keeps working
  through a network drop.
- **Minimal dependencies** — aiohttp is the server's only third-party package; the client
  adds Vite, React, and Dexie and nothing else (no TypeScript, no state-management
  framework, no charting or ADIF libraries). This runs on a club laptop dusted off once a
  year, where a small dependency tree and dumb code age better.
- **Trust model** — no authentication; the LAN is the boundary. Anyone can edit or delete
  any Contact — net control fixing someone else's typo is normal — so accountability comes
  from stamping, not permissions: every Contact carries the Operator callsign and initials
  it was logged under, plus the Client UUID of the machine that last edited it. The Admin
  page is gated by a shared password sent as a header: a tripwire to stop people messing
  around, explicitly not a security mechanism. Nothing here is safe to expose to the
  internet; doing so would need a new decision.

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

The data loop is offline-first and converges by repetition rather than by protocol:

1. The client writes new and edited Contacts to IndexedDB as `pending`. UUIDs are
   client-generated, so a Contact can be created with the server unreachable.
2. **Push** — an idempotent POST upsert keyed on that UUID, retried every ~10s while
   anything is pending. Duplicate sends are harmless.
3. **Pull** — a GET for everything changed since the client's sync cursor, every ~30s and
   immediately on a poke.
4. **Pull is the acknowledgment** — a Contact becomes `synced` only when the server echoes
   it back, which makes the loop self-healing with no extra ack protocol.

Conflicts resolve last-write-wins on the server only; the client applies whatever a pull
returns. Two operators editing the same Contact at the same moment is rare enough that
CRDTs or field-level merge aren't worth carrying. Deletes are soft — a flag, never a removed
row, which also suits an auditable contest log. The sync cursor is always a **server-time**
timestamp; client clocks are never trusted for sync.

**Event identity gates all of it.** The client compares its Event UUID against the server's
at boot and on every `event` message. A mismatch means the server loaded a different Event,
and the client stops and makes the operator choose rather than mixing or wiping logs on its
own.

**Callsign lookup** is its own REST endpoint, answered by the server from local datasets and
a cache that outlives Events. The record shape is the contract; the sources behind it are
not — see [SERVER.md](./SERVER.md).

**Log interchange runs client-side.** ADIF export and import both operate on the browser's
own replica of the log; the server has no ADIF code and no export or import endpoint — see
[CLIENT.md](./CLIENT.md).
