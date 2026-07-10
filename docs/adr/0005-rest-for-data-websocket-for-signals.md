# REST carries data; one WebSocket carries signals

Contact data flows exclusively over HTTP: push (idempotent POST upsert), pull (GET since
sync cursor), and event config (GET). Each client also holds a single WebSocket used only
for signals: presence heartbeats, chat, and server "pokes" that prompt an immediate pull
when contacts change.

The WebSocket is a pure optimization. If it is down, nothing breaks: pulls continue on a
30s timer, pending pushes retry every 10s, presence goes stale, chat pauses. The sync
loop (ADR-0001) never depends on the socket — this keeps the offline-first guarantee
honest and the failure modes boring.

Presence is heartbeat-based, not connection-based: clients send their status (Client
UUID, operator, band, mode) every 5 seconds and on change; the server keeps it in memory
only and relays it. Other clients derive liveness from heartbeat recency ("last seen Ns
ago") and drop stale stations. No connected-clients table in the database — presence is
ephemeral runtime state and would be stale the moment the server restarts.

Chat handles network blips by brute force: on any detected connection issue the client
re-fetches the *entire* chat history over REST and replaces its local chat state — an
Event's chat is small enough that cursors and gap-fill logic aren't worth their
complexity. Messages still carry client-generated UUIDs (they are the primary key, and
they make any resend harmless). An outgoing message absent from the post-reconnect
history didn't arrive: it is marked failed in the UI for the operator to resend manually.
There is no automatic retry queue for chat — that machinery is reserved for Contacts.

## Considered options

- Contact sync over the WebSocket too — rejected: makes the socket load-bearing and
  duplicates the retry/ack machinery REST already gives us.
- Presence persisted in a database table (original spec) — rejected in favor of
  in-memory, since nobody queries historical presence and restarts would leave lies.
