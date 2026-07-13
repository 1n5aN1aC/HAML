# Template fields stored in a JSON column; Event config frozen at creation

Contacts have fixed columns for the universal fields (UUID, contact time, created/edited
timestamps, both callsigns, initials, computer name, band, mode, `deleted`) plus a single
JSON column holding all Template-defined field values. Every Event database therefore has
an identical schema, which keeps server code, sync, and the back/save/load logic uniform.

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

- Dynamic real columns (`ALTER TABLE` per event) — viable given one-DB-per-event, but
  makes schemas differ across events for little gain; rejected.
- EAV side-table — worst ergonomics for whole-contact sync; rejected.
- Additive-only mid-event field changes — rejected in favor of full immutability, which
  eliminates the config-sync problem entirely.
