# HAML — API reference

Every route the server serves, and every message the WebSocket carries. This is the
wire contract; the reasoning behind it lives in [SERVER.md](./SERVER.md) and
[ARCHITECTURE.md](./ARCHITECTURE.md). Terminology: [GLOSSARY.md](./GLOSSARY.md).

Routes are registered in `api_rest.setup_routes` and `api_ws.setup`.

## Conventions

**Everything is JSON**, request and response, with `Content-Type: application/json` on
any request carrying a body.

**Errors are `{"error": "<message>"}`** at the matching status. The one exception is a
path no route matches, where aiohttp serves its own plain-text 404 — worth knowing when
a client parses error bodies blindly.

**Timestamps are ISO-8601 UTC with milliseconds** (`2026-06-27T18:04:11.482+00:00`).
Anything the client sends is normalized to that form on arrival, so `Z`-suffixed input
is accepted. Every data response carries `server_time`, sampled after the query.

**No authentication except on `/api/admin/*`**, which requires the shared password in an
`X-Admin-Password` header and answers `401` when it doesn't match. The LAN is the
boundary — see [ARCHITECTURE.md](./ARCHITECTURE.md), *Trust model*. The password is
configured by `admin_password` (default `haml`).

**Most data routes need an active Event.** With none loaded they answer
`404 {"error": "no active event"}`. `POST /api/lookup` and the admin surface are the
exceptions: they work with no Event, which is what lets the client's no-Event screen fix
the situation. On the admin routes the password is checked *first*, so a bad password on
a server with no Event is `401`, not `404`.

## Route summary

| Method   | Path                                    | Needs Event | Admin |
| -------- | --------------------------------------- | ----------- | ----- |
| `GET`    | `/api/event`                            | yes         |       |
| `POST`   | `/api/contacts`                         | yes         |       |
| `GET`    | `/api/contacts`                         | yes         |       |
| `GET`    | `/api/chat`                             | yes         |       |
| `POST`   | `/api/lookup`                           | no          |       |
| `GET`    | `/api/admin/templates`                  | no          | yes   |
| `GET`    | `/api/admin/templates/{template_id}`    | no          | yes   |
| `PUT`    | `/api/admin/templates/{template_id}`    | no          | yes   |
| `DELETE` | `/api/admin/templates/{template_id}`    | no          | yes   |
| `GET`    | `/api/admin/events`                     | no          | yes   |
| `POST`   | `/api/admin/events`                     | no          | yes   |
| `POST`   | `/api/admin/events/{event_uuid}/activate` | no        | yes   |
| `DELETE` | `/api/admin/events/{event_uuid}`        | no          | yes   |
| `POST`   | `/api/admin/backup`                     | yes         | yes   |
| `DELETE` | `/api/admin/chat`                       | yes         | yes   |
| `GET`    | `/api/admin/lookup-cache`               | no          | yes   |
| `DELETE` | `/api/admin/lookup-cache`               | no          | yes   |
| `GET`    | `/ws`                                   | no          |       |

In production the server also serves the built client: `GET /` returns
`client/dist/index.html` and everything else falls through to static files from
`client/dist`. Both routes exist only when that directory does; in development the Vite
dev server proxies `/api` and `/ws` here instead.

## Event

### `GET /api/event`

The active Event's metadata and its frozen configuration. The client calls this at boot
and caches the result per Event UUID forever — the config cannot change for the life of
an Event, so there is no re-check and no config-sync path.

```json
{
  "event_uuid": "8a1e…",
  "name": "Field Day 2026",
  "station_callsign": "W7XYZ",
  "local_exchange": "W7XYZ 6A OR",
  "config": {
    "fields": [ … ],
    "bands": ["160m", "80m", …],
    "modes": ["Phone", "CW", "Digital"],
    "duplicate_type": "band-mode",
    "location": {"latitude": 45.0, "longitude": -123.0},
    "export": null
  },
  "server_time": "2026-06-27T18:04:11.482+00:00"
}
```

`local_exchange` and `location` are null when the Event was created without them.
`export` is the reserved export-mapping slot, unused today. The `fields` list is the
Template's, copied at creation — its item shape is documented under
[SERVER.md](./SERVER.md), *Template*, and [GLOSSARY.md](./GLOSSARY.md), *Field*.

- `404` — no active Event.

## Contacts

### `POST /api/contacts`

Upsert one Contact, keyed on its client-generated UUID. Idempotent: resending an
identical body is harmless, which is what lets the client retry blindly.

Required keys — all of them, or `400`:

| Key                 | Type    | Notes                                        |
| ------------------- | ------- | -------------------------------------------- |
| `uuid`              | string  | client-generated; the upsert key             |
| `qso_at`            | string  | ISO-8601                                     |
| `last_edited`       | string  | ISO-8601; the LWW conflict clock             |
| `remote_callsign`   | string  | non-empty                                    |
| `operator_callsign` | string  | non-empty                                    |
| `operator_initials` | string  | non-empty                                    |
| `client_uuid`       | string  | non-empty; the machine that last edited      |
| `band`              | string  | non-empty                                    |
| `mode`              | string  | non-empty                                    |
| `deleted`           | boolean | soft-delete flag                             |
| `fields`            | object  | custom Template field values; `{}` when none |

Optional: `created_at` (preserved from the stored row on an update, stamped now when
absent), and any **built-in field** as a top-level string —
`country`, `itu_zone`, `cq_zone`, `continent`, `gridsquare`, `distance`, `state`,
`section`, `county`, `frequency`, `rst_sent`, `rst_received`, `name`, `comment`. Absent
built-ins default to `''`; a present one that isn't a string is `400`. Every string
value is stripped.

```json
{"stored": true, "server_time": "2026-06-27T18:04:11.482+00:00"}
```

`stored` is `false` when the incoming `last_edited` lost to a newer stored version —
last-write-wins, resolved here and nowhere else. A `false` is a success, not an error:
the client leaves the row `pending` and the next pull delivers the winning version.
A stored write broadcasts a `poke` to every socket.

- `400` — body isn't JSON, isn't an object, a required key is missing, a value is the
  wrong type, a timestamp won't parse, or a **required** Template field is blank.
  Required-field enforcement mirrors the client: it applies only to fields with both
  `entry` and `required` set, and tombstones (`deleted: true`) are exempt so a deletion
  can always sync.
- `404` — no active Event.

The server never rejects a Contact for being a duplicate. Dupe checking is client-side
and advisory ([CLIENT.md](./CLIENT.md), *Dupes*).

### `GET /api/contacts[?since=<timestamp>]`

Everything changed since the cursor, oldest first, soft-deleted rows included. Without
`since`, the whole log.

The comparison is `synced_at >= since` — **inclusive**, so the boundary row comes back
every time. That's deliberate and harmless given the upsert, and it means a cursor can
never skip a row written in the same millisecond.

```json
{
  "contacts": [
    {
      "uuid": "…", "qso_at": "…", "created_at": "…", "last_edited": "…",
      "synced_at": "…", "remote_callsign": "W7ABC", "operator_callsign": "K7XYZ",
      "operator_initials": "JC", "client_uuid": "…", "band": "20m", "mode": "Phone",
      "country": "United States", "itu_zone": "6", "cq_zone": "3", "continent": "NA",
      "gridsquare": "CN84", "distance": "79", "state": "OR", "section": "OR",
      "county": "Polk", "frequency": "14.250", "rst_sent": "59", "rst_received": "59",
      "name": "Alex", "comment": "",
      "deleted": false,
      "fields": {"class": "3A"}
    }
  ],
  "server_time": "2026-06-27T18:04:11.482+00:00"
}
```

Built-ins are top-level strings (`''` when unset); custom Template values live in
`fields`. `deleted` is a real boolean here, unlike the integer in storage.

**`server_time` is the next cursor**, and the only valid source of one — it is sampled
after the query, so nothing can slip between the read and the stamp. The client also
derives its clock offset from it. A push response's `server_time` must never be used as
a cursor; it is stamped before later writes land.

- `400` — `since` won't parse as a timestamp.
- `404` — no active Event.

## Chat

### `GET /api/chat`

The Event's entire chat history, oldest first. There is no pagination and no `since` —
a client recovering from any connection blip re-fetches the whole thing and replaces its
local state, which is why chat needs no retry or acknowledgment machinery.

```json
{
  "messages": [
    {
      "uuid": "…", "sent_at": "2026-06-27T18:03:02.114+00:00",
      "operator_callsign": "K7XYZ", "operator_initials": "JC",
      "client_uuid": "…", "text": "coffee's on"
    }
  ],
  "server_time": "2026-06-27T18:04:11.482+00:00"
}
```

Messages are *sent* over the WebSocket, not here. `sent_at` is server-stamped.

- `404` — no active Event.

## Callsign lookup

### `POST /api/lookup`

```json
{"callsign": "W7ABC"}
```

The callsign is uppercased and stripped of `/P`, `/M`, `/MM`, `/QRP`, `/ANT`, and any
trailing `/` before anything else happens, so those variants share one cache key and one
answer.

**The response shape is the contract; the sources behind it are not.** The client trusts
these field names and value types without validating them and knows nothing about where
the server got them. `200` returns the canonical record — every key below always
present, `null` when unknown:

`callsign`, `source`, `fetched_at`, `name`, `license_type`, `license_class`,
`previous_callsign`, `previous_license_class`, `trustee_callsign`, `trustee_name`,
`address_line1`, `address_line2`, `address_attn`, `state`, `county`, `country`,
`continent`, `latitude`, `longitude`, `gridsquare`, `itu_zone`, `cq_zone`, `dxcc`,
`frn`, `grant_date`, `expiry_date`

plus `distance` — kilometers from the Event's operating position, added at request time
and `null` when either end has no coordinates. It is deliberately not part of the stored
record: it depends on which Event is active, so it must never freeze into a cache row.

Types are normalized before you see them: empty strings become `null`, strings are
stripped, `license_type`/`license_class`/`previous_license_class` are lowercased,
`latitude`/`longitude` are floats, `itu_zone` (1–90), `cq_zone` (1–40), and `dxcc`
(1–999) are integers, `grant_date`/`expiry_date` are `YYYY-MM-DD`, `state` is a 2-letter
USPS code, and `gridsquare` is a 4-character Maidenhead grid. `continent` is passed
through as the source wrote it, conventionally uppercase. A value that's present but
won't coerce lands as `null` rather than a surprise — and shortens the cache row's TTL.

- `404` — no source knew the callsign.
- `408` — the long-poll ceiling (15s) elapsed. Nothing is cached; retry freely.
- `400` — body isn't JSON, or `callsign` is missing or empty after normalization.
- `502` — every source missed and at least one errored; the message is the first
  error's. A missing FCC dataset reads as an error, not a miss, so a broken install
  says so instead of quietly answering "not found".

**Lookup is optional machinery.** A miss, error, or timeout leaves the operator's fields
blank; logging is never blocked or delayed by one. Concurrent requests for the same
callsign share a single upstream hit. How the chain of sources behind this endpoint
works is [SERVER.md](./SERVER.md), *Callsign lookup*.

## Admin — Templates

Template files are read from `server/templates/`. Overwriting or deleting one cannot
affect a live Event, which holds a frozen copy of its configuration. A `template_id` is
a bare filename stem — anything with a path separator is rejected as unknown.

### `GET /api/admin/templates`

`{"templates": [{"id": "field-day", "name": "ARRL Field Day"}]}` — every valid template
file, sorted by id. A file that doesn't parse or doesn't validate is skipped silently
rather than breaking the listing, and `example` is hidden: it's living documentation on
disk, not a usable contest definition.

### `GET /api/admin/templates/{template_id}`

The template's full JSON, for the editor. `404` when it doesn't exist or doesn't parse.

### `PUT /api/admin/templates/{template_id}`

Create or overwrite, body being the whole template. Returns `{"id": …, "name": …}`.
`400` with the specific complaint when the id isn't `[a-z0-9_-]+` or the body fails
validation — a missing `name`, an empty `bands`/`modes`, an unknown `duplicate_type`,
a duplicate field name, a custom field without `label`/`max_length`, a built-in
reference trying to redefine `label`/`max_length`/`validation`, or a bad `validation`
regex.

### `DELETE /api/admin/templates/{template_id}`

`{"deleted": "<id>"}`, or `404`.

## Admin — Events

### `GET /api/admin/events`

Every Event database on disk. Unreadable files are skipped with a server-side warning.

```json
{"events": [{
  "event_uuid": "…", "name": "Field Day 2026", "station_callsign": "W7XYZ",
  "local_exchange": "W7XYZ 6A OR", "template_name": "ARRL Field Day",
  "created_at": "…", "active": true
}]}
```

### `POST /api/admin/events`

Create an Event from a Template and activate it immediately. Every connected client sees
the resulting `event` broadcast and stops to make its operator choose.

```json
{
  "name": "Field Day 2026",
  "station_callsign": "W7XYZ",
  "template": "field-day",
  "local_exchange": "W7XYZ 6A OR",
  "location": {"latitude": 45.0, "longitude": -123.0}
}
```

`name`, `station_callsign`, and `template` are required; the callsign is uppercased.
`local_exchange` is optional, uppercased, and display-only. `location` is optional but
must be exactly `latitude` + `longitude` as numbers in range when present — it's the
reference point for lookup distances, and without it no distances are shown.

`201` with the same meta shape as the listing, minus `active`. `400` on any validation
failure, including a template that doesn't load.

### `POST /api/admin/events/{event_uuid}/activate`

Switch the server to a stored Event and broadcast the change. Returns the active Event
object (`event_uuid`, `name`, `station_callsign`, `local_exchange`, `config`). `404`
when no Event has that UUID.

### `DELETE /api/admin/events/{event_uuid}`

Delete an Event's database file. `{"deleted": "<uuid>"}`, `404` when unknown, `400` for
the **active** Event — its file is held open and clients are logging into it. Activate
something else first.

## Admin — maintenance

### `POST /api/admin/backup`

Snapshot the **active** Event via SQLite's backup API — safe while open — into
`<data_dir>/backups/<slug>-<YYYYmmdd-HHMMSS>.db`. Returns `{"backup": "<filename>"}`.
`404` when no Event is active; there is no way to back up an inactive one, since its
file is already a complete, quiescent copy.

### `DELETE /api/admin/chat`

Delete every chat message in the Event and broadcast `chat_cleared`. Returns
`{"cleared": true}`. The single exception to chat being append-only. `404` when no Event
is active.

### `GET /api/admin/lookup-cache`

`{"ok": 12, "not_found": 3, "error": 0}` — raw row counts by status, expired rows
included, because that's what the clear button acts on.

### `DELETE /api/admin/lookup-cache`

`{"cleared": true, "deleted": 15}`.

## WebSocket — `GET /ws`

One socket per client, carrying **signals only**. Contact data never travels here. The
socket is pure optimization: with it down, pulls continue on their timer, pending pushes
keep retrying, presence goes stale, and chat pauses — see
[ARCHITECTURE.md](./ARCHITECTURE.md), *How client and server talk*.

Every frame is a JSON object with a `type`. Non-text frames, unparseable frames, and
unknown types are ignored silently. The server pings every 30s; a client is expected to
reconnect with backoff on close.

**On connect** the server immediately sends an `event` message followed by a
`presence_list`.

### Client → server

**`presence`** — the heartbeat, sent every 5s and immediately on any change. All five
keys must be non-empty strings or the frame is dropped without complaint.

```json
{"type": "presence", "client_uuid": "…", "callsign": "K7XYZ",
 "initials": "JC", "band": "20m", "mode": "Phone"}
```

**`chat`** — send a message. Server-stamped, stored, and broadcast to everyone including
the sender. The UUID is client-generated, and the insert ignores a UUID it already has,
so a resend is harmless. Dropped silently when no Event is active or any key is blank.

```json
{"type": "chat", "uuid": "…", "operator_callsign": "K7XYZ",
 "operator_initials": "JC", "client_uuid": "…", "text": "coffee's on"}
```

### Server → client

**`event`** — the active Event's UUID, or `null` when none is loaded. Sent on connect
and whenever an admin creates or activates an Event. **This is how a client notices an
Event switch**: it compares against its own and stops rather than mixing logs.

```json
{"type": "event", "event_uuid": "8a1e…"}
```

**`presence_list`** — the full roster, rebroadcast on every heartbeat received.

```json
{"type": "presence_list", "stations": [
  {"client_uuid": "…", "callsign": "K7XYZ", "initials": "JC",
   "band": "20m", "mode": "Phone", "last_seen_at": 1782415451.2}
]}
```

`last_seen_at` is **epoch seconds**, not an ISO string — the one place in the API that
isn't. Liveness is heartbeat recency, never socket state: an entry ages out of server
memory after 120s without a heartbeat, and clients apply their own, tighter staleness
cutoff for display. Presence is memory-only; a server restart empties the roster.

**`chat`** — one stored message, in the same shape `GET /api/chat` returns.

```json
{"type": "chat", "message": {"uuid": "…", "sent_at": "…", … }}
```

**`poke`** — "contacts changed", sent after any stored upsert. Prompts an immediate pull
instead of waiting for the next tick. Carries no payload and no information: sync is
correct without it.

```json
{"type": "poke"}
```

**`chat_cleared`** — an admin wiped the history; drop the whole local copy, including
pending and failed outgoing messages.

```json
{"type": "chat_cleared"}
```
