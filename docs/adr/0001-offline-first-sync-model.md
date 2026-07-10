# Offline-first sync: LWW upserts, pull-as-acknowledgment, timestamp cursor

Clients log Contacts to a local IndexedDB first and sync to the server in the background,
so logging keeps working through network drops. We chose the simplest sync machinery that
converges, accepting imperfect conflict handling because simultaneous edits of the same
Contact are possible but expected to be very rare.

- **Client-generated UUIDs** identify Contacts, so records can be created offline.
- **Push is an idempotent upsert** keyed on UUID; the client retries `pending` Contacts
  until confirmed. Duplicate sends are harmless.
- **Pull is the acknowledgment.** A Contact becomes `synced` only when the server echoes
  it back in a pull. Push responses are not trusted as confirmation — this makes the loop
  self-healing with no extra ack protocol.
- **Conflicts resolve Last-Write-Wins on `last_edited`**, on both server and client. A
  pulled record older than a local `pending` copy does not clobber it. Losing edits are
  silently dropped; state converges on the next pull.
- **Deletes are soft.** A `deleted` flag plus a `last_edited` bump syncs like any edit.
  Rows are never hard-deleted (also desirable for an auditable contest log).
- **The sync cursor is a server-time timestamp**, returned by the server in every pull
  response and stored by the client. Client clocks are never trusted for sync (skewed
  client clocks would otherwise create permanent sync holes). Pull queries are inclusive
  (`>= cursor`); boundary re-fetches are harmless due to idempotent upserts.
- **Implementation note:** the cursor compares against a *server-stamped* `synced_at`
  column, set on every stored change — not against the client-supplied `last_edited`,
  which would leak client clocks back into the cursor. `last_edited` is only the LWW
  conflict clock. Consequently a cursor is only valid if taken from a *pull* response
  (stamped after the query), never from a push response (stamped after the row).

## Considered options

- Server-assigned monotonic sequence number as the cursor — more robust than timestamps,
  rejected to keep the server stateless about sync progress and the schema minimal.
- CRDT / field-level merge / surfacing conflicts to the operator — rejected as
  over-engineering for a near-zero-contention domain.
