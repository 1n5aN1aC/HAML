# One Event per database; server runs exactly one Event at a time

The server, its database, and each connected client all operate on exactly one Event at a
time — the contacts table has no `event_id` column. The server provides logic to back up,
save, and load Event databases, so switching events is an explicit administrative action,
not a query-time filter. This keeps every query trivial, makes archiving an event a file
copy, and matches the operating model of club logging software (one file per contest).

Clients detect an Event switch via the Event UUID: on mismatch they warn the operator,
then wipe local state and pull fresh configuration if the operator agrees. There is no
silent migration of local data between Events.

## Considered options

- Single database with an `event_id` column and a "currently active event" pointer —
  rejected: complicates every query and sync path to support a multi-tenancy we don't
  want; archiving and resetting become delete operations instead of file operations.
