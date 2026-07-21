# Log interchange is client-side ADIF

Both directions of log interchange — full ADIF export and ADIF import — run entirely in the
client, against its own Dexie copy of the log. The server has no export or import endpoint
and no ADIF code at all; the parser and the writer are hand-written in
`client/src/adif.js` and `client/src/adif-export.js`.

Putting it in the client falls out of offline-first (ADR-0001). Every client already holds a
complete replica of the Event log, so an export is a local read: it works with the server
down, needs no new API surface, and adds no dependency to either side (ADR-0006). The cost
is that a client can only export what it has replicated, so the export dialog states how
many of the contacts it is about to write are still `pending` and lets the operator decide
whether to sync first.

**The operator resolves band and mode, not the code.** An Event's band and mode lists are
arbitrary Template strings ('Phone', 'Digital', 'Other', 'FT2') while ADIF's are fixed
enumerations, so neither direction can be a static lookup table. Both dialogs list every
distinct value found — in the file, or in the log — with its count, seed a guess from an
alias table, and refuse to run until anything unseeded has been chosen explicitly. Field
Day's catch-all 'Other' is the case that makes guessing indefensible: only the operator
knows what was actually on the air.

**Export never silently drops a field.** Built-in columns map to their real ADIF tags
(`ARRL_SECT`, `CQZ`, `ITUZ`, `CNTY`, …); anything without a mapping — a newly added
built-in, an admin-invented custom field — is written as `APP_HAML_<NAME>` rather than
skipped. The record loop iterates the built-in registry and the Event's field list rather
than each contact's own keys, so the output stays column-stable across contacts and a
schema addition can't quietly vanish from an export.

**Import lands on the normal sync path.** Accepted records are written to IndexedDB as
`pending` and pushed by the sync engine exactly like hand-logged contacts — no bulk server
endpoint, no second write path to keep correct. Three filters run first, all reported in the
dialog before anything is written: records with no callsign or no parseable timestamp are
unusable; records matching an existing contact on callsign, band, mode, and minute are
skipped as duplicates; and records that can't satisfy one of the Event's required fields
(from the file, or from that field's Template default) are skipped, because the server would
reject them and they would sit `pending` forever. The operator can also shift every
timestamp by a signed days/hours/minutes offset, for the common case of a source logging
computer whose clock was wrong.

## Considered options

- A server-side export endpoint (the original plan) — rejected: the server would have to
  learn ADIF, and it buys nothing, since the client already holds the whole log and the
  operator has to resolve the band/mode mappings interactively anyway. Worth reconsidering
  only if export ever needs to run unattended.
- Guessing band/mode automatically and letting the operator correct afterwards — rejected:
  a wrong mode in a submitted log is worse than one extra screen, and the failure is silent.
- Importing by POSTing the file to the server — rejected: it duplicates the upsert path
  Contacts already have, and it wouldn't work offline.
- An ADIF library — rejected under the same minimal-dependency rule as the rest of the stack
  (ADR-0006); the subset we emit and parse is small and fully specified.
