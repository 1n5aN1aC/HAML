# HAML — Design Overview

HAML (HAM Logger) is a client-server web application for logging amateur radio contacts
during an event (Field Day, POTA, …) — the web-based, modern successor to the N3FJP
workflow. A Python server on a club laptop serves a React client to operator positions
over a trusted LAN. Terminology: [CONTEXT.md](./CONTEXT.md). Decisions: [adr/](./adr/).

## Architecture at a glance

- **Server**: Python + aiohttp (sole dependency), stdlib sqlite3. One database file per
  Event; the server runs exactly one Event at a time, with admin logic to back up, save,
  and load Event databases. Serves the built client's static files in production.
  [ADR-0002](./adr/0002-one-event-per-database.md),
  [ADR-0006](./adr/0006-tech-stack.md)
- **Client**: React (plain JavaScript), built with Vite (dev server proxies API + WS to
  aiohttp). Offline-first: contacts live in IndexedDB (via Dexie) and sync in the
  background. State is React context/hooks.
- **Trust model**: no authentication; the LAN is the boundary. Anyone can edit or delete
  any Contact; accountability via last-editor stamping (Operator callsign, initials,
  Client UUID). The Admin page is gated by a simple shared password — a tripwire, not
  security. [ADR-0004](./adr/0004-no-auth-trusted-lan.md)

## Data model

**Contact** — fixed columns: UUID (client-generated), QSO date/time, created / last-edited
timestamps, station callsign context, remote callsign, Operator callsign + initials and
Client UUID of the **last editor**, band, mode, `deleted` flag — plus one JSON column for
all Template-defined field values. [ADR-0003](./adr/0003-json-field-storage-frozen-event-config.md)

**Template** (JSON file in `templates/`, built-ins shipped) defines per-contest: extra
Contact fields (`text` / `number` / `choice`; name, label, required, default, order),
band/mode lists, a dupe key, and a reserved export-mapping slot (empty in v1). Creating
an Event copies the template config into the Event database; the Event's configuration is
**frozen at creation** — clients cache it per Event UUID forever.

**Event switch**: clients compare the server's Event UUID on every connection; on
mismatch they warn the operator, then wipe local state and pull fresh config.

## Sync (see [ADR-0001](./adr/0001-offline-first-sync-model.md))

New/edited contacts are written to IndexedDB as `pending`, then:

1. **Push** — idempotent POST upsert keyed on UUID, retried every ~10s while pending.
2. **Pull** — GET contacts with `last_edited >= cursor`, every ~30s and immediately on a
   server poke. The cursor is a **server-time** timestamp returned in each pull response.
3. **Pull is the ack**: a contact becomes `synced` only when the server echoes it back.
   Conflicts resolve last-write-wins on `last_edited` on the server only; the client
   applies pulled records unconditionally. Deletes are soft (`deleted` flag).

The client also keeps a rolling **clock offset** (server time − local time, sampled from
pull responses) and defaults new QSO times from server-corrected time, operator-editable.

## Real-time (see [ADR-0005](./adr/0005-rest-for-data-websocket-for-signals.md))

REST carries data; one WebSocket per client carries signals — and the socket is pure
optimization; everything degrades gracefully to polling without it.

- **Presence**: heartbeat every 5s and on change (Client UUID, operator, band, mode);
  server memory only; peers show "last seen Ns ago" and drop stale stations.
- **Chat**: append-only, stored in the Event DB. Live messages over WS; on connect or
  any connection issue, the client re-fetches the entire history over REST (it's small).
  Sends that don't survive a blip are marked failed for manual resend.
- **Pokes**: "contacts changed" nudges that trigger an immediate pull.

## Client UI

Top **status bar**: Operator callsign + initials inputs, band/mode dropdowns, server
connection indicator (top right). Logging is disabled until all are filled.

**Left pane** (larger): top half is the Event-wide contact list (rendered window: most
recent ~50; search box later), each row showing its sync-state indicator; clicking a
contact opens an edit/delete modal. Bottom half is the entry form — universal fields plus
the Template's fields, with entry-time dupe warnings (never blocking).

**Right pane**: online stations with band/mode and last-seen (top); scrolling chat box
(middle); reserved empty box for future use (bottom).

## Explicitly deferred

- Cabrillo/ADIF export (data shape is export-capable; endpoint is post-v1)
- Contact list search/filtering
- Any internet-facing deployment (would require a new security decision)
