# Callsign lookup is an ordered chain of sources with a post-processing stage

Server-side callsign lookup is a list of interchangeable **sources** in
`lookup.SOURCES`, walked in order, plus a single **post-processing** stage every
answer passes through on the way out. A source is a plain module — no class, no
registration call; it becomes part of the chain by being listed. The contract is
written down in `server/lookup_blank.py`: `SOURCE`, `CACHED`, `setup(app)`, an
optional `close(app)`, and `lookup(app, callsign) -> {status, payload, error}`,
which may be sync or `async def` (the dispatcher awaits an awaitable result, so
an online provider needs no change to the chain).

The shipped chain is `fcc` → `blank` → `callparser`. `blank` always misses; it
exists so the module contract is expressed in code, and so the position an
online provider (QRZ, HamQTH, ACMA, HamCall — see TODO.md) belongs in already
exists: after the free offline US hit, before the prefix-DB fallback.

**A source declines by missing.** There is no routing predicate — every source
sees every callsign, and `not_found` is how it says "not mine". A prefix filter
would be a second way to say no, and a wrong one would silently blackhole calls;
a source that must not waste a paid API call on a US callsign can check the
prefix inside its own `lookup()`.

**An error falls through but is not forgotten.** A source returning `error` —
FCC's sqlite missing, an online provider timing out — does not abort the chain,
because a server with no FCC dataset should still answer DX calls from the
prefix DB. But it must not vanish into a `not_found` either: a missing dataset
that reads as "callsign not found" is undiagnosable. So the chain remembers the
**first** error and returns it only if nothing below resolves. That is exactly
the split the client already sees: any OK → 200; all miss, none errored → 404;
all miss, at least one errored → 502 with the first error's message. A source
that raises is caught and presented as an error, so one broken module cannot
take the chain down.

**Each source declares whether it may be cached; the dispatcher does the
writing.** `CACHED` is a property of the source, not of the result, and no
source touches `lookup_cache` itself — one writer, one place the TTL policy
lives. Both shipped sources are `CACHED = False`: they are offline and answer in
microseconds, so a row buys nothing on latency and costs correctness, because
the cache is read once *before* the chain runs. That pre-chain read is
deliberate — it is what makes a cache worth having — but it means a cached row
outranks every source above the one that wrote it until the row expires. Paying
that only for genuinely expensive sources is the point of the flag.

`dirty` is not plumbed through the source result yet. Nothing shipped is
cacheable, so every write would be clean; when the first real caching source
lands, add it to the result shape (both offline adapters already compute
`bad_fields`) so a half-coerced record gets the 15-minute TTL instead of 365
days.

**Post-processing runs after the cache, on every OK path.** `lookup_postprocess`
is one file that does whatever it wants and hands back the record. It runs on
cache hits and fresh results alike, *after* the cache has been read or written,
so: the cache stores what a source actually said, derivation changes take effect
with no cache to clear, and request-relative values never get frozen into a row
that outlives the Event that produced them. Its input is the canonical record
(`lookup_record.FIELDS`) — the storage contract; its output is the wire shape,
those fields plus request-time extras. Today it derives CQ/ITU zones from
coordinates (only-fill-if-null, so CallParser's authoritative prefix-DB zones
win) and stamps `distance` from the active Event's operating position. Both
moved here — the zone derivation out of the FCC adapter, the distance out of
`api_rest` — because they apply to every source at once rather than being
reimplemented per adapter. The remaining location work in TODO.md (derive a
location from grid/country, override one from state or a POTA park) belongs
here for the same reason.

None of this is visible above `api_rest.post_lookup`. The record shape is the
client's contract; the sources behind it are not (see DESIGN.md, *Callsign
lookup*).
