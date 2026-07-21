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
position (e.g. "Radio 1's laptop"). It survives Event switches — it names the machine, not
the Event. A Contact stamps the Client UUID of the machine that **last edited** it,
overwritten on every edit; the Operator callsign and initials on a Contact are the ones it
was logged under and carry over through edits unless the editor retypes them.
_Avoid_: computer name, hostname, station ID

**Event location**:
The latitude/longitude of the operating position, set once at Event creation and stored in
the Event's frozen configuration. Optional; it is the reference point for the distance to a
looked-up station, so when it is unset no distances are shown.
_Avoid_: my grid, site coordinates

**Local exchange**:
The full exchange string this station sends on the air (e.g. "W7XYZ 6A OR"), set
once at Event creation and shown verbatim in the client's status bar. Optional and
display-only for now; when unset the status bar falls back to the Station callsign.
Distinct from any per-contact `exchange` **Field**, which is what you *receive* from
the other station.
_Avoid_: my exchange, sent exchange

**Admin page**:
The Admin tab of the web client, protected by a simple shared password — a tripwire against
accidental misuse, not a security mechanism. It covers Events (create one from a Template,
activate a stored one, delete an inactive one, back up the active one), Templates (create,
edit, and delete the template files on the server), the callsign-lookup cache (row counts,
and clearing it), and maintenance actions (clear this Event's chat history, add test
contacts). When the server has no active Event, the client shows this page and nothing
else — it's the only thing that can fix that state.

**Field**:
An item in a Template's ordered `fields` list, describing one input on a Contact (e.g.
"ARRL Section"). Every Field has a machine `name` and two required booleans: `entry` (it
appears in the entry form) and `history` (it appears as a contact-list column) — a Field may
be either, both, or neither, and list order is the one order both surfaces share. Optional
on any Field: `required`, `default`, and `remember` (on entry, re-fill this field from the
most recent local contact with the same callsign, whoever logged it).

A Field is one of two kinds. A **custom field** carries its own presentation too — `label`,
`max_length`, and an optional `validation` object (`pattern` plus the `message` shown when
it fails) — and its values live in the Contact's JSON `fields` column. A **built-in
reference** names a Built-in field instead, and may *not* restate label, max length, or
validation; those come from the client's registry.

An Event's field set is frozen at creation.
_Avoid_: column, attribute

**Built-in field**:
One of a fixed roster of per-Contact values that every Event database has a real column for,
whether or not its Template mentions them: country, ITU zone, CQ zone, continent,
gridsquare, distance, state, section, county, frequency, RST sent, RST received, name, and
comment. All are optional and default to empty; many of them are machine-fillable, and
arrive by **Lookup autofill** unless the operator types them. The roster lives in `server/db.py`
(`BUILTIN_FIELDS`); the client's registry (`client/src/builtin-fields.js`) owns each one's
label, max length, and validation. A Template controls only whether and where a built-in is
shown, by referencing it in its `fields` list. `comment` is the one free-text built-in —
every other field is uppercased and space-free.
_Avoid_: standard field, system field

**Callsign lookup**:
Asking the server what it knows about a remote callsign. The client fires one as the
operator types a callsign; the server answers with a single record of station details —
name, location, zones, and the like — or says it doesn't know. How the server finds that
record, and from where, is the server's business and is expected to change.
_Avoid_: callbook, QRZ (in code), the name of any particular data source

**Lookup autofill**:
Filling a Contact's Built-in fields from the Callsign lookup record. Only fields the
operator hasn't typed into are filled, and only values the field's own validation accepts;
anything else stays blank. Autofill is best-effort and never blocks logging — a failed or
slow lookup just means the operator types the fields.
_Avoid_: enrichment, prefill

**Lookup cache**:
The server's store of previously fetched lookup records. It lives outside the Event
databases and outlives them — callsign facts belong to no particular Event. The Admin page
reports its row counts and can clear it.

**Dupe**:
A Contact that matches an existing Contact in the Event under the Template's duplicate
type: `band-mode` (same callsign, band, and mode; the default), `any` (same callsign),
`band-mode-day` (band-mode within the current UTC day), or `none` (checking off). Dupes
are warned about at entry time by the client, never blocked, and never rejected by the
server.
_Avoid_: duplicate contact, collision

**Event UUID**:
The identity of an Event. Clients compare it against the server's at boot and on every
`event` message from the WebSocket. A mismatch means the server loaded a different Event;
the client stops and makes the operator choose — switch (wipe the local store, take the new
Event), continue offline against the cached old Event, or first export the local data as
JSON. It never wipes or mixes logs on its own.

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
A message stamped with Operator callsign/initials, Client UUID, and server timestamp.
Stored in the Event database: history belongs to the Event, is archived with it, and is
wiped on an Event switch. Append-only in normal use — messages are never edited and never
individually deleted — with one exception: the Admin page can clear the whole history at
once, which deletes every row and tells clients to drop their local copies.

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

**ADIF export**:
Writing this client's copy of the Event log to a standard `.adi` file, entirely in the
browser. Because an Event's band and mode names are arbitrary strings, the operator maps
each onto an ADIF enumeration value before the file is written. Distinct from a **backup**,
which is a server-side copy of the whole Event database.
_Avoid_: log dump, download

**ADIF import**:
Reading an `.adi`/`.adif` file from another logger and turning its records into Contacts.
The operator maps the file's modes and bands onto the Event's, picks the operator identity
to log them under, and may shift every timestamp to correct a wrong clock on the source
machine. Imported Contacts enter the local store as `pending` and sync like any other.
_Avoid_: merge, restore
