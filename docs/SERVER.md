# HAML — Server

Python + aiohttp (the sole third-party dependency) over stdlib sqlite3, running on a club
laptop. It holds the authoritative log, answers callsign lookups, relays real-time signals,
and in production serves the built client's static files — aiohttp covers all three server
needs, which is why it's the only dependency. Validation is hand-written; the API surface is
small enough that a framework doesn't pay its way.

High-level picture: [ARCHITECTURE.md](./ARCHITECTURE.md).
Client side: [CLIENT.md](./CLIENT.md).
Terminology: [GLOSSARY.md](./GLOSSARY.md).

## Events and databases

One database file per Event, and the server runs exactly one Event at a time — the contacts
table has no `event_id` column, so every query is trivial and archiving an Event is a file
copy. Switching Events is an explicit administrative action, not a query-time filter. (The
conventional alternative — one database with an `event_id` column and an active-Event
pointer — was rejected: it complicates every query and sync path to support a multi-tenancy
we don't want, and turns archiving and resetting into delete operations.)

The lookup cache and the reference datasets sit in their own files *outside* the Event
databases, so they survive Event switches — callsign facts belong to no particular Event.

## Data model

**Contact** — fixed columns: UUID (client-generated), QSO date/time, created / last-edited
timestamps, remote callsign, Operator callsign + initials of the **logging operator**, the
Client UUID of the **last editor's machine** (overwritten on every edit), band, mode, and a
`deleted` flag — plus a fixed roster of **built-in field** columns (country, ITU zone, CQ
zone, continent, gridsquare, distance, state, section, county, frequency, RST sent/received,
name, comment) and one JSON column holding the Event's *custom* Template fields. The Station
callsign is Event metadata in the `meta` table, not a per-Contact column. Giving each Event
its own dynamic columns would fit one-DB-per-Event, but making schemas differ between Events
buys little; the built-ins are the opposite trade, and that's why they work — one fixed set
of columns, identical everywhere, grown only by additive migration.

The built-in roster is declared once in `server/db.py` (`BUILTIN_FIELDS`) and mirrored by the
client's display registry (`client/src/builtin-fields.js`), which owns each one's label, max
length, and validation; a smoke test keeps the two honest. Every built-in is optional and
defaults to `''`. Adding one is an additive migration — `open_db` ALTERs any missing built-in
column onto an existing Event database when it opens it.

Every Event database therefore has an identical schema, which keeps sync and the
backup/save/load logic uniform.

**Template** — a JSON file in `server/templates/`, with built-ins shipped (Field Day, POTA).
It defines per-contest: one ordered `fields` list, band and mode lists, a duplicate type, and
a reserved export-mapping slot (unused). Each `fields` item is either a **custom field**
(machine name + `label` + `max_length`, optional `validation` regex with its own message,
values landing in the JSON column) or a **built-in reference** (just the built-in's name —
label, max length, and validation come from the client registry and may not be overridden,
values landing in that built-in's own column). Either kind carries two required booleans,
`entry` and `history`, plus optional `required`, `default`, and `remember`.

Creating an Event copies the Template's configuration into the Event database and adds the
Event's own **location** — the operating position's latitude/longitude, optional, and the
reference point for lookup distances. The Event's configuration is **frozen at creation**, so
clients can cache it per Event UUID indefinitely and there is no config-change sync path at
all. Later edits to Template files cannot affect a live Event. Allowing additive mid-Event
field changes was considered; full immutability was chosen because it removes the
config-sync problem entirely, at the price of "forgot a field" meaning "make a new Event".

## Sync endpoints

- **Push** — POST, an idempotent upsert keyed on the Contact UUID. Duplicate sends are
  harmless. The server never rejects a Contact for being a dupe; dupe checking is client-side
  advisory only.
- **Pull** — GET everything with `synced_at >= cursor`; the response carries the new cursor.
  The comparison is inclusive, so boundary rows are re-fetched — harmless, given the upsert.
  `synced_at` is **server-stamped on every stored change**, which is what keeps client clocks
  out of the cursor. `last_edited` is only the conflict clock. A cursor is therefore valid
  only when taken from a pull response (stamped after the query), never from a push response.
  A server-assigned sequence number would be a sturdier cursor than a timestamp, but it was
  rejected to keep the server stateless about each client's sync progress.
- **Conflicts** resolve last-write-wins on `last_edited`, on the server only. Deletes are
  soft: a `deleted` flag plus a `last_edited` bump. Rows are never hard-deleted, which also
  suits an auditable contest log.

## Real-time

One WebSocket per client, carrying signals only (see
[ARCHITECTURE.md](./ARCHITECTURE.md), *How client and server talk*):

- **Presence** — clients heartbeat every 5s and on change (Client UUID, operator, band,
  mode). The server holds this **in memory only** and relays it; there is no
  connected-clients table, since presence would be stale the moment the server restarts.
- **Chat** — stored in the Event database, so history is archived with the Event and wiped on
  a switch. Live messages go out over the socket; clients recovering from a blip re-fetch the
  entire history over REST. Append-only, with one exception: the Admin **clear chat** action
  deletes every row and broadcasts `chat_cleared`.
- **Pokes** — "contacts changed" nudges that prompt an immediate pull instead of waiting for
  the next tick.

The server also broadcasts an `event` message carrying the active Event UUID, which is how
clients notice an Event switch.

## Callsign lookup

The client asks about a remote callsign while the operator types it, over its own REST
endpoint (`api_rest.post_lookup`). The server returns one record — station name, location,
zones, country, and a distance from the Event's location — or reports that it has nothing.

**The record shape is the contract; the sources behind it are not.** The client trusts the
record's field names and value types without validating them and knows nothing about where
the server got them. Where the data comes from, how the server chains sources, and what it
can answer for non-US callsigns are all expected to change; nothing above `post_lookup`
changes with them.

Lookup is optional machinery, like the WebSocket: a lookup that misses, errors, or times out
leaves the fields blank for the operator to fill. Logging is never blocked or delayed by one.

### How the chain works

Lookup is an ordered list of interchangeable **sources** in `lookup.SOURCES`, walked in
order, plus a single **post-processing** stage every answer passes through on the way out.

A source is a plain module — no class, no registration call; it becomes part of the chain by
being listed. The contract is written down in `server/lookup_blank.py`: `SOURCE`, `CACHED`,
`setup(app)`, an optional `close(app)`, and `lookup(app, callsign) -> {status, payload,
error}`, which may be sync or `async def` (the dispatcher awaits an awaitable result, so an
online provider needs no change to the chain).

The shipped chain is **`fcc` → `blank` → `callparser`**. `blank` always misses; it exists so
the module contract is expressed in code, and so the slot an online provider (QRZ, HamQTH,
ACMA, HamCall) belongs in already exists: after the free offline US hit, before the
prefix-DB fallback.

- **A source declines by missing.** There is no routing predicate — every source sees every
  callsign, and `not_found` is how it says "not mine". A source that must not waste a paid
  API call on a US callsign checks the prefix inside its own `lookup()`.
- **An error falls through but is not forgotten.** A source returning `error` — FCC's sqlite
  missing, an online provider timing out — does not abort the chain, but it does not vanish
  into a `not_found` either. The chain remembers the **first** error and returns it only if
  nothing below resolves. That yields exactly what the client sees: any OK → 200; all miss,
  none errored → 404; all miss, at least one errored → 502 with the first error's message. A
  source that raises is caught and presented as an error, so one broken module cannot take
  the chain down.
- **Each source declares whether it may be cached**; the dispatcher does the writing. `CACHED`
  is a property of the source, not of the result, and no source touches `lookup_cache` itself
  — one writer, one place the TTL policy lives. Both shipped *data* sources, `fcc` and
  `callparser`, are `CACHED = False`: they are offline and answer in microseconds, and the
  cache is read once *before* the chain runs, so a cached row would outrank every source
  above whichever one wrote it until it expired. `blank` is `CACHED = True` purely as the
  worked example; its write path never fires, because it never returns an OK.

**Post-processing runs after the cache, on every OK path.** `lookup_postprocess` is one file
that takes the canonical record (`lookup_record.FIELDS` — the storage contract) and hands
back the wire shape: those fields plus request-time extras. It runs on cache hits and fresh
results alike, *after* the cache has been read or written, so the cache stores what a source
actually said, derivation changes take effect with no cache to clear, and request-relative
values never freeze into a row that outlives the Event that produced them. Today it derives
CQ/ITU zones from coordinates (only-fill-if-null, so CallParser's authoritative prefix-DB
zones win) and stamps `distance` from the active Event's operating position. The remaining
location work in [TODO.md](../TODO.md) — deriving a location from grid or country, overriding
one from state or a POTA park — belongs here too, since it applies to every source at once.

`dirty` is not plumbed through the source result yet: no shipped source ever writes a cache
row, so there is nothing for it to describe. When the first real caching source lands, add it
to the result shape (both offline adapters already compute `bad_fields`) so a half-coerced
record gets the 15-minute TTL instead of 365 days.

Supporting modules: `lookup_fcc.py` (local FCC ULS dataset), `lookup_callparser.py` over
`callparser.py` (prefix DB), `lookup_zones.py` (CQ/ITU polygons), `lookup_cache.py`. Dataset
provenance and schemas: [server/datasets/README.md](../server/datasets/README.md).

## Admin surface

Every admin request carries a shared password as a header — a tripwire to stop people messing
around, explicitly not a security mechanism (see [ARCHITECTURE.md](./ARCHITECTURE.md),
*Trust model*). Behind it: create an Event from a Template;
activate, back up, or delete a stored Event; create, edit, and delete Template files; inspect
and clear the callsign-lookup cache; clear the Event's chat history; and inject test contacts.

When the server has no active Event, there is nothing to serve but this surface — the client
shows the Admin page alone, since it's the only thing that can fix that state.

## Testing

Three smoke tests in `server/tests/`:
- `smoke.py` (events, sync, admin)
- `smoke_ws.py` (presence, chat, pokes)
- `smoke_lookup.py` (callsign lookup)

Each spawns its own server on a scratch port with a scratch data directory, so nothing needs to be running first;
Each prints a check count and exits non-zero on failure. Run them one at a time — see [INSTALL.md](../INSTALL.md).
