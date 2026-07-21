# Custom Template fields in a JSON column; built-ins as real columns; Event config frozen at creation

Contacts have fixed columns for the universal fields (UUID, contact time, created/edited
timestamps, remote callsign, Operator callsign + initials, Client UUID, band, mode,
`deleted`), a further fixed roster of **built-in field** columns, and a single JSON column
holding every *custom* Template-defined field value. Every Event database therefore has an
identical schema, which keeps server code, sync, and the backup/save/load logic uniform.
(The Station callsign is Event metadata in the `meta` table, not a Contact column — it is
the same for every Contact in the Event.)

**Built-in fields** are the contest-agnostic per-Contact values that recur across nearly
every contest, several of them machine-fillable: country, ITU zone, CQ zone, continent,
gridsquare, distance, state, section, county, frequency, RST sent, RST received, name, and
comment. The roster is declared once in `server/db.py` (`BUILTIN_FIELDS`) and mirrored by
the client's display registry (`client/src/builtin-fields.js`), which owns each one's
label, max length, and validation pattern; a smoke test keeps the two lists honest. Every
built-in is optional and defaults to `''`, so a Template that references none of them costs
nothing. Adding one to the roster is an additive migration — `open_db` ALTERs any missing
built-in column onto an existing Event database when it opens it.

A Template's `fields` list therefore holds two kinds of item. A **custom field** carries its
own complete definition and its values land in the JSON column. A **built-in reference** is
the built-in's name plus per-Event flags only — it may not redefine label, max length, or
validation, which come from the client registry — and its values land in that built-in's
own column. Both kinds sit in one ordered list and independently opt into the entry form
(`entry`) and the contact list (`history`).

Templates are JSON files in a `templates/` directory on the server (with built-ins such
as Field Day and POTA). Creating an Event copies the template's configuration — field
definitions, band/mode lists, and duplicate type — into the Event database, making the Event
self-contained: later edits to template files cannot affect a live Event.

An Event's configuration is **frozen at creation**. Forgetting a field means creating a
new Event. Consequence: clients can fetch the event config once per Event UUID and cache
it indefinitely; there is no config-change sync path at all.

Dupe checking is client-side advisory only: the Template defines a duplicate type
(`band-mode`, `any`, `band-mode-day`, or `none`); the client warns at entry time but
never blocks, and the server never enforces uniqueness.

## Considered options

- Dynamic real columns per Event (`ALTER TABLE` for each Template's own fields) — viable
  given one-DB-per-event, but makes schemas differ across events for little gain; rejected.
  The built-in roster is the opposite trade, and is why it works: one fixed set of columns,
  identical in every Event database, grown only by additive migration.
- Keeping the built-ins in the JSON blob alongside custom fields (the original design) —
  rejected once they became machine-filled and shared across contests: real columns let the
  server query and index them, and give export and statistics a stable shape that doesn't
  depend on what a particular Template happened to declare.
- EAV side-table — worst ergonomics for whole-contact sync; rejected.
- Additive-only mid-event field changes — rejected in favor of full immutability, which
  eliminates the config-sync problem entirely.
