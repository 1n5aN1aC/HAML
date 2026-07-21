# HAML — Client

React in plain JavaScript (no TypeScript), built with Vite — whose dev server proxies API and
WebSocket traffic to aiohttp during development, and whose production build emits static files
for aiohttp to serve. IndexedDB access goes through Dexie; state is plain React context and
hooks, no state-management framework. [ADR-0006](./adr/0006-tech-stack.md)

High-level picture: [ARCHITECTURE.md](./ARCHITECTURE.md). Server side:
[SERVER.md](./SERVER.md). Terminology: [GLOSSARY.md](./GLOSSARY.md).

## Local store and sync

The client is **offline-first**: every Contact is written to IndexedDB first and synced in the
background, so logging survives a network drop.
[ADR-0001](./adr/0001-offline-first-sync-model.md)

New and edited Contacts land as `pending` and are pushed on a ~10s retry until the server
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
from the WebSocket. On mismatch it stops and makes the operator choose
([ADR-0002](./adr/0002-one-event-per-database.md)):

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
Event's local exchange. Logging is disabled until identity, band, and mode are filled — the
band dropdown starts on **Off-Air**, a pseudo-band that counts as unfilled and keeps the
station out of band-conflict checks. Bands other live stations occupy are marked inside the
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
  entry, and an incoming chat message.

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
- **Admin** — the password-gated Admin page
  ([ADR-0004](./adr/0004-no-auth-trusted-lan.md)): Events, Templates, lookup cache, and
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

**Deferred**: contest-submission export (Cabrillo, or ADIF shaped to a contest's rules). The
Settings tab's "Export Event for Submission" button is present but disabled.
