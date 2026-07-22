# HAML — Client

React in plain JavaScript (no TypeScript), built with Vite — whose dev server proxies API and
WebSocket traffic to aiohttp during development, and whose production build emits static files
for aiohttp to serve. IndexedDB access goes through Dexie; state is plain React context and
hooks, no state-management framework. Charts and ADIF are hand-rolled rather than pulled in
as libraries.

High-level picture: [ARCHITECTURE.md](./ARCHITECTURE.md). Server side:
[SERVER.md](./SERVER.md). Terminology: [GLOSSARY.md](./GLOSSARY.md).

## Local store and sync

The client is **offline-first**: every Contact is written to IndexedDB first and synced in the
background, so logging survives a network drop. Contact UUIDs are generated here, which is
what lets a Contact exist before the server has ever seen it.

New and edited Contacts land as `pending` and are pushed on a ~10s retry — backing off
exponentially to ~2min while the server is unreachable, and snapping back to ~10s on the
first success — until the server
echoes them back in a pull, which is the only thing that marks them `synced`. Pulls run every
~30s and immediately on a server poke. The client applies pulled records **unconditionally** —
the server is the source of truth, and there is no client-side conflict logic. Accepted risk:
a local pending edit can be overwritten by an older pulled copy in the small window before its
push lands.

The client also keeps a rolling **clock offset** (server time − local time, sampled from pull
responses) and defaults new QSO times from server-corrected time, operator-editable.

Event configuration is fetched once per Event UUID and cached forever — it's frozen at
creation server-side, so there is nothing to re-check.

## Event switch

The client compares its Event UUID against the server's at boot and on every `event` message
from the WebSocket. On mismatch it stops and makes the operator choose:

- **Switch** — wipe every local trace of the old Event (contacts, chat, sync cursor, clock
  offset), then pull the new configuration.
- **Continue offline** — keep logging locally against the cached old Event, with sync and
  presence disabled so nothing is lost and nothing is mixed. A reload brings the choice back.
- **Export local data** — download the cached Event, contacts, and chat as JSON before
  deciding.

Nothing is wiped without the operator choosing it. The Client UUID and the operator's identity
survive a switch — the machine and the person didn't change.

## UI

A persistent **top bar** carries the tab nav (**Logging**, **Stats**, **Settings**, **Admin**),
the active Event name, the theme picker, and the server connection indicator. The background
machinery — sync, socket, presence, chat — lives above the tabs, so it keeps running whichever
tab is shown.

### Logging tab

Opens with a **status bar**: Operator callsign + initials inputs, band/mode dropdowns, and the
Event's local exchange. Logging is disabled until callsign, initials, and band are filled — the
band dropdown starts on **Off-Air**, a pseudo-band that counts as unfilled and keeps the
station out of band-conflict checks. Mode never gates anything: it starts on the Event's
'Phone' or, failing that, its first mode, and a saved mode the current Event doesn't list
falls back the same way. Bands other live stations occupy are marked inside the
dropdown, and sharing a band with another station raises an inline warning.

**Left pane** (larger): the top half is the Event-wide contact list (rendered window: most
recent ~50, newest first), each row showing its sync-state indicator; clicking a contact opens
an edit/delete modal. The 🔍 in its header opens a search box that filters the whole local log
rather than just the visible window — every space-separated token must match somewhere among
the displayed columns, case-insensitively, so "w7 ssb" finds W7ABC on SSB. The bottom half is
the entry form: callsign plus the Template's `entry` fields, with entry-time dupe warnings
(never blocking).

**Entry ergonomics.** The entry row is built for typing, not clicking:

- **Keyboard** — Space moves to the next field (log values never contain spaces) and wraps
  around the row, as does Tab; Escape wipes the in-progress contact and returns to the callsign
  box; Enter logs it. A freetext field — `comment` — takes a literal space instead.
- **Fills** — a lookup fires as the callsign settles and autofills the built-ins it can;
  `remember` fields refill from the last contact with that callsign (anyone's, not just this
  operator's); section and state derive each other. Every fill skips fields the operator has
  typed into, and changing the callsign clears the untouched ones so one station's data can't
  carry into the next. Only values the field's own validation accepts are filled.
- **Feedback** — a running UTC/local clock, the looked-up country and distance beside the
  callsign box, and distinct sounds for a normal log, a DX log, a dupe warning, a rejected
  entry, and an incoming chat message. Distance is always *stored* in kilometers; a display
  may convert it.

**Right pane**: online stations with band/mode and last-seen (top); a scrolling chat box
(middle); a panel toggling between the section map and a compact stats readout (bottom). The
section map colors worked sections from the local log and can list the missing ones.

**Presence and chat**: peers are shown as "last seen Ns ago" and dropped when stale,
liveness coming from heartbeat
recency rather than socket state. On connect or any connection issue the client re-fetches the
entire chat history over REST and replaces its local state; an outgoing message absent from the
post-reconnect history is marked failed for manual resend, since there is no automatic retry
queue for chat. On `chat_cleared` the client drops its whole local history, including its own
pending and failed messages.

**Dupes** are checked client-side only, under the Template's duplicate type (`band-mode`,
`any`, `band-mode-day`, or `none`). The client warns at entry time and never blocks; the server
never enforces uniqueness.

### Other tabs

- **Stats** — the fuller statistics view: detailed stats panel, QSO rate graph, section map.
- **Settings** — client settings and log interchange (below).
- **Admin** — the password-gated Admin page: Events, Templates, lookup cache, and
  maintenance actions. Also shown on its own when the server has no active Event. What it can
  do is listed in [SERVER.md](./SERVER.md).

## Log interchange (ADIF)

Both directions run **client-side**, from the browser's own Dexie copy of the log — the server
has no export or import endpoint and no ADIF code at all; the parser and writer are hand-written
in `client/src/adif.js` and `client/src/adif-export.js`. Since every client already holds a
complete replica, an export is a local read: it works with the server down and adds no API
surface and no dependency to either side. The cost is that a client can only export what it has
replicated, so the export dialog states how many of the contacts it is about to write are still
`pending` and lets the operator decide whether to sync first.

**The operator resolves band and mode, not the code.** An Event's band and mode lists are
arbitrary Template strings ('Phone', 'Digital', 'Other', 'FT2') while ADIF's are fixed
enumerations, so neither direction can be a static lookup table. Both dialogs list every
distinct value found — in the file, or in the log — with its count, seed a guess from an alias
table, and refuse to run until anything unseeded has been chosen explicitly. Field Day's
catch-all 'Other' is the case that makes guessing indefensible: only the operator knows what
was actually on the air.

- **Export** (Settings tab) writes the whole Event log as a standard `.adi` file. Built-in
  columns map to their real ADIF tags (`ARRL_SECT`, `CQZ`, `ITUZ`, `CNTY`, …) and anything
  unmapped falls back to `APP_HAML_<NAME>`, so a newly added built-in or an admin-invented
  custom field can't silently vanish. The record loop iterates the built-in registry and the
  Event's field list rather than each contact's own keys, keeping output column-stable across
  contacts.
- **Import** (Settings tab) parses an `.adi`/`.adif` file from another logger, then has the
  operator choose the operator identity to log under and optionally shift every timestamp
  (signed days/hours/minutes) to correct a wrong clock on the source machine. Three filters run
  first, all reported before anything is
  written: records with no callsign or no parseable timestamp are unusable; records matching an
  existing contact on callsign, band, mode, and minute are skipped as duplicates; and records
  that can't satisfy one of the Event's required fields (from the file, or from that field's
  Template default) are skipped, because the server would reject them and they'd sit `pending`
  forever. Accepted rows are written to IndexedDB as `pending` and reach the server over the
  normal sync path — the same road hand-logged contacts take.
- **Raw export** (Settings tab) writes a `.json` snapshot of everything the browser holds for
  the Event — the cached config, the chat, and every contact row as stored, tombstones and
  unsynced rows included. It isn't an interchange format and nothing reads it back; it exists
  for archiving and for handing a log to someone who needs to see what the client actually has.
  The mismatch rescue screen's "Export local data" button produces the same file.

## Submission export

A third Settings button, **Export Event for Submission**, writes the file a contest sponsor
wants rather than the whole log. It is driven entirely by the Template's `export` block
(documented in `server/templates/example.json`), which names a writer in
`client/src/submission-export.js` and supplies its parameters. A Template without one leaves
the button disabled and says so.

Where the ADIF export asks the operator to resolve every band and mode, this one asks nobody:
the admin who wrote the Template knew which contest it is, so the mapping tables live there.
That split is why the two writers stay independent rather than sharing mappings — the same
`Phone` becomes `SSB` for ADIF and `PH` for a future Cabrillo, and only the Template knows
which is wanted.

`export.fields` is the **entire** record, in order. Each name resolves against a prompt
answer, then Event meta, then a contact column, then a custom Template field; a name that
resolves to nothing is left out of the record, as is one whose value is blank. `band_map` and
`mode_map` are complete by contract — a log value they don't list writes no tag, and the
dialog warns about it by name and count first. That is how a catch-all like Field Day's
`Other`, which only the operator can interpret, fails visibly rather than being guessed at.

What the log never recorded, the dialog asks for: `export.prompts` are ordinary field
definitions rendered by the same `FieldInput` the entry form uses, so they validate the same
way, and their answers are addressable from `fields`. POTA asks for the activated park and
the operator's state. The filename follows the sponsor's convention
(`K7ABC@US-1234-20260620.adi`) but stays editable, which is what covers the cases the schema
doesn't model — a park spanning two states, a two-fer, one club position's share of a log.

Nothing validates a Template's export block, on either side. A required prompt left blank is
the only thing that blocks an export; everything else warns and lets the operator proceed,
the same stance the entry form takes on dupes.

**Deferred**: Cabrillo, for Field Day and Winter Field Day. The design carries it — the
ordered `fields` list is a Cabrillo QSO line, and `band_map` becomes band-to-kHz — but it
would add a `choices` key on prompts for the category dropdowns, and header emission owned by
the writer.
