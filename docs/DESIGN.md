# HAML — Design Overview

HAML (HAM Logger) is a client-server web application for logging amateur radio contacts
during an event (Field Day, POTA, …) — the web-based, modern successor to the N3FJP
workflow. A Python server on a club laptop serves a React client to operator positions
over a trusted LAN. Terminology: [CONTEXT.md](./CONTEXT.md). Decisions: [adr/](./adr/).

## Architecture at a glance

- **Server**: Python + aiohttp (sole dependency), stdlib sqlite3. One database file per
  Event; the server runs exactly one Event at a time, with admin logic to back up, save,
  and load Event databases. It also answers callsign lookups (see
  [Callsign lookup](#callsign-lookup)), whose cache and reference data sit in their own
  files outside the Event databases. Serves the built client's static files in production.
  [ADR-0002](./adr/0002-one-event-per-database.md),
  [ADR-0006](./adr/0006-tech-stack.md)
- **Client**: React (plain JavaScript), built with Vite (dev server proxies API + WS to
  aiohttp). Offline-first: contacts live in IndexedDB (via Dexie) and sync in the
  background. State is React context/hooks.
- **Trust model**: no authentication; the LAN is the boundary. Anyone can edit or delete
  any Contact; accountability via stamping — the Operator callsign and initials a Contact
  was logged under, plus the Client UUID of the machine that last edited it. The Admin page
  is gated by a simple shared password — a tripwire, not security.
  [ADR-0004](./adr/0004-no-auth-trusted-lan.md)

## Data model

**Contact** — fixed columns: UUID (client-generated), QSO date/time, created / last-edited
timestamps, remote callsign, Operator callsign + initials of the
**logging operator** (the edit modal carries them over, so an editor must retype them to
take ownership), Client UUID of the **last editor's machine** (overwritten on every edit),
band, mode, `deleted` flag — plus a fixed roster of **built-in field** columns (country,
ITU/CQ zone, continent, gridsquare, distance, state, section, county, frequency, RST
sent/received, name, comment) and one JSON column for the Event's *custom* Template fields.
The Station callsign is Event metadata, not a per-Contact column.
[ADR-0003](./adr/0003-json-field-storage-frozen-event-config.md)

**Template** (JSON file in `templates/`, built-ins shipped) defines per-contest: one ordered
`fields` list, band/mode lists, a duplicate type, and a reserved export-mapping slot (unused
— see [Log interchange](#log-interchange-adif)). Each `fields` item is either a **custom
field** (machine name + `label` + `max_length`, optional `validation` regex with its own
message) or a **built-in reference** (just the built-in's name — label, max length, and
validation come from the client's registry and may not be overridden). Either kind carries
two required booleans, `entry` (show in the entry form) and `history` (show as a
contact-list column), plus optional `required`, `default`, and `remember` (re-fill this
field from the most recent local contact with the same callsign — anyone's, not just this
operator's). List order is the one order the entry form and the contact list share. Creating
an Event copies the template config into the Event database and adds the Event's own
**location** — the operating position's latitude/longitude, entered at creation, optional,
and the reference point for lookup distances. The Event's configuration is **frozen at
creation** — clients cache it per Event UUID forever.

**Event switch**: clients compare the server's Event UUID at boot and on every `event`
message from the WebSocket. On mismatch the client stops and makes the operator choose:
**switch** (wipe all local data for the old Event, then pull fresh config), **continue
offline** (keep logging locally against the cached old Event — sync and presence stay off,
and a reload brings the choice back), or **export local data** (download the cached event,
contacts, and chat as JSON first). Nothing is wiped without the operator choosing it.

## Sync (see [ADR-0001](./adr/0001-offline-first-sync-model.md))

New/edited contacts are written to IndexedDB as `pending`, then:

1. **Push** — idempotent POST upsert keyed on UUID, retried every ~10s while pending.
2. **Pull** — GET contacts with `synced_at >= cursor`, every ~30s and immediately on a
   server poke. The cursor is a **server-time** timestamp returned in each pull response;
   `synced_at` is server-stamped on every stored change, while `last_edited` is only the
   conflict clock.
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
- **Chat**: stored in the Event DB, append-only in normal use. Live messages over WS; on
  connect or any connection issue, the client re-fetches the entire history over REST (it's
  small). Sends that don't survive a blip are marked failed for manual resend. The one
  exception to append-only is the Admin page's **clear chat**, which deletes every message
  server-side and broadcasts `chat_cleared` so clients drop their local copies.
- **Pokes**: "contacts changed" nudges that trigger an immediate pull.

## Callsign lookup

The client asks the server about a remote callsign while the operator types it, over its
own REST endpoint, and merges the answer into the entry form (see **Entry ergonomics** in
[Client UI](#client-ui)). The server returns one record per callsign — station name, location,
zones, country, and a distance from the Event's location — or reports that it has nothing.

**The record shape is the contract; the sources behind it are not.** The client trusts the
record's field names and value types without validating them, and knows nothing about where
the server got them. Lookup answers are cached server-side, outside the Event databases, so
the cache survives Event switches. Where the data comes from, how the server chains
sources, and what it can answer for non-US callsigns are all expected to change; nothing
above this line should have to change with them.

Lookup is optional machinery, like the WebSocket: a lookup that misses, errors, or times
out leaves the fields blank for the operator to fill, and logging is never blocked or
delayed by one.

## Client UI

A persistent **top bar** carries the tab nav (**Logging**, **Stats**, **Settings**,
**Admin**), the active Event name, the theme picker, and the server connection indicator.
The background machinery (sync, socket, presence, chat) lives above the tabs, so it keeps
running whichever tab is shown.

The **Logging** tab opens with a **status bar**: Operator callsign + initials inputs,
band/mode dropdowns, and the Event's local exchange. Logging is disabled until identity,
band, and mode are filled — the band dropdown starts on **Off-Air**, a pseudo-band that
counts as unfilled and keeps the station out of band-conflict checks. Bands other live
stations occupy are marked inside the band dropdown, and sharing a band with another
station raises an inline warning.

**Left pane** (larger): top half is the Event-wide contact list (rendered window: most
recent ~50, newest first), each row showing its sync-state indicator; clicking a contact
opens an edit/delete modal. The 🔍 in its header opens a search box that filters the whole
local log rather than just the visible window — every space-separated token must match
somewhere among the displayed columns, case-insensitively, so "w7 ssb" finds W7ABC on SSB.
Bottom half is the entry form — callsign plus the Template's `entry` fields, with
entry-time dupe warnings (never blocking).

**Entry ergonomics.** The entry row is built for typing, not clicking:

- **Keyboard**: Space moves to the next field (log values never contain spaces) and wraps
  around the row, as does Tab; Escape wipes the in-progress contact and returns to the
  callsign box; Enter logs it. A freetext field — `comment` — takes a literal space instead.
- **Fills**: a lookup fires as the callsign settles and autofills the built-ins it can;
  `remember` fields refill from the last contact with that callsign; section and state
  derive each other. Every fill skips fields the operator has typed into, and changing the
  callsign clears the untouched ones so one station's data can't carry into the next.
- **Feedback**: a running UTC/local clock, the looked-up country and distance beside the
  callsign box, and distinct sounds for a normal log, a DX log, a dupe warning, a rejected
  entry, and an incoming chat message.

**Right pane**: online stations with band/mode and last-seen (top); scrolling chat box
(middle); a panel toggling between the section map and a compact stats readout (bottom).
The section map colors worked sections from the local log and can list the missing ones.

The **Stats** tab holds the fuller statistics view (detailed stats panel, QSO rate graph,
section map). The **Settings** tab holds client settings and log interchange. The **Admin**
tab is the password-gated Admin page
([ADR-0004](./adr/0004-no-auth-trusted-lan.md)) — Events, Templates, lookup cache, and
maintenance actions; it's also shown on its own when the server has no active Event.

## Log interchange (ADIF)

Both directions run **client-side**, from the browser's own Dexie copy of the log — the
server has no export or import endpoint
([ADR-0007](./adr/0007-client-side-adif-interchange.md)).

- **Export** (Settings tab) writes the whole Event log as a standard `.adi` file. Built-in
  columns map to their real ADIF tags (`ARRL_SECT`, `CQZ`, `ITUZ`, `CNTY`, …) and anything
  unmapped falls back to `APP_HAML_<NAME>`, so a newly added field can't silently vanish
  from an export. Because an Event's band/mode names are arbitrary strings, the operator
  maps each onto an ADIF enumeration value before the file is written; the dialog also
  reports how many contacts are still unsynced, since a client can only export what it
  holds.
- **Import** (Settings tab) parses an `.adi`/`.adif` file from another logger, then has the
  operator map its modes and bands onto the Event's lists, choose the operator identity to
  log under, and optionally shift every timestamp (signed days/hours/minutes) to correct a
  wrong clock on the source machine. Rows that would duplicate an existing contact (same
  callsign, band, mode, and minute) are skipped, as are rows that can't satisfy one of the
  Event's required fields. Accepted rows are written to IndexedDB as `pending` and reach the
  server over the normal sync path — the same road hand-logged contacts take.
- **Raw export** (Settings tab) writes a `.json` snapshot of everything the browser holds for the
  Event — the cached config, the chat, and every contact row as stored, tombstones and unsynced rows
  included. It isn't an interchange format and nothing reads it back; it exists for archiving and
  for handing a log to someone who needs to see what the client actually has. The mismatch rescue
  screen's "Export local data" button produces the same file.

## Explicitly deferred

- Contest-submission export (Cabrillo, or ADIF shaped to a contest's rules) — the Template's
  export-mapping slot exists for it but is unimplemented, and the Settings tab's "Export
  Event for Submission" button is present but disabled.
- Any internet-facing deployment (would require a new security decision)
