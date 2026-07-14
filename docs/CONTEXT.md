# HAML (HAM Logger)

A client-server web application for logging amateur radio contacts during an event
(e.g. Field Day, POTA), with an offline-first React client syncing to a Python server.

## Language

**Contact**:
A single logged QSO — one exchange with a remote station, stored as one record with a
client-generated UUID.
_Avoid_: QSO (in code), log entry, record

**Template**:
A reusable definition of a contest type (e.g. Field Day, POTA): the list of
contest-specific Contact fields and any other per-contest configuration.
_Avoid_: contest definition, schema, profile

**Event**:
A concrete instance of a Template — e.g. "Field Day 2026". The server, its database, and
each client operate on exactly one Event at a time. Identified by an Event UUID generated
at creation.
_Avoid_: contest (when the instance is meant), session

**Operator**:
The person at the mic, identified by their personal callsign plus initials, entered in
the status bar. Distinct from the Station callsign.
_Avoid_: user, logger

**Station callsign**:
The callsign transmitted on the air for the whole Event (e.g. the club call at Field
Day), set once in Event configuration. For solo operation it may equal the Operator's
callsign.
_Avoid_: club call (in code), my callsign

**Client UUID**:
A UUID each client generates for itself and persists locally, identifying the logging
position (e.g. "Radio 1's laptop"). Contacts stamp the Client UUID, Operator callsign,
and initials of whoever **last edited** them.
_Avoid_: computer name, hostname, station ID

**Local exchange**:
The full exchange string this station sends on the air (e.g. "W7XYZ 6A OR"), set
once at Event creation and shown verbatim in the client's status bar. Optional and
display-only for now; when unset the status bar falls back to the Station callsign.
Distinct from any per-contact `exchange` **Field**, which is what you *receive* from
the other station.
_Avoid_: my exchange, sent exchange

**Admin page**:
A page in the web client for administrative actions (create/load/backup Events),
protected by a simple shared password — a tripwire against accidental misuse, not a
security mechanism.

**Field**:
A Template-defined input on a Contact (e.g. "ARRL Section"). Has a machine name, label,
type (`text`, `number`, or `choice`), required flag, default value, and display order.
An Event's field set is frozen at creation.
_Avoid_: column, attribute, custom field

**Dupe**:
A Contact that matches an existing Contact in the Event under the Template's duplicate
type: `band-mode` (same callsign, band, and mode; the default), `any` (same callsign),
`band-mode-day` (band-mode within the current UTC day), or `none` (checking off). Dupes
are warned about at entry time by the client, never blocked, and never rejected by the
server.
_Avoid_: duplicate contact, collision

**Event UUID**:
The identity of an Event. Clients compare it against the server's on every connection; a
mismatch means the server loaded a different Event, and the client must warn the operator
and wipe its local store before continuing.

**Soft delete**:
A Contact is never removed from the database; deletion sets a `deleted` flag and bumps
`last_edited`, so the deletion syncs like any other change. Clients never display
soft-deleted Contacts.
_Avoid_: tombstone, hard delete

**Sync state**:
Client-side status of a Contact: `pending` (local change not yet confirmed by the server)
or `synced` (the server has echoed this version back in a pull).
_Avoid_: dirty flag, upload status

**Presence**:
A client's heartbeat over the WebSocket — Client UUID, Operator callsign, initials,
band, and mode — sent every 5 seconds and immediately on change. Held in server memory
only; clients display every other station with a "last seen N seconds ago" and drop
stale ones. Liveness is heartbeat recency, never socket state.
_Avoid_: connected-clients table, online status

**Poke**:
A tiny WebSocket notification from the server ("contacts changed") prompting clients to
Pull immediately instead of waiting for the next poll tick. Pure optimization — sync
never depends on it.
_Avoid_: push notification, event

**Chat message**:
An append-only message stamped with Operator callsign/initials, Client UUID, and server
timestamp. Stored in the Event database: history belongs to the Event, is archived with
it, and is wiped on an Event switch.

**Pull**:
The client's periodic query for all Contacts changed since its sync cursor. The pull is
also the *acknowledgment* mechanism: a Contact only becomes `synced` when the server
echoes it back in a pull.

**Push**:
The client's fire-and-retry upload of `pending` Contacts. An idempotent upsert keyed on
UUID. Push alone never marks a Contact `synced`.

**Sync cursor**:
The server-time timestamp the client stores after each successful pull, used as the
`since` parameter of the next pull. Always server clock, never client clock.
