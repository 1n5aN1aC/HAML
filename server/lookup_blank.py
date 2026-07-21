"""Blank lookup source: the slot a future online provider fills.

Always misses. It exists so the shape of a source module is written down in
code rather than in a comment, and so the chain in `lookup.SOURCES` already
has the position an online provider (QRZ, HamQTH, ACMA, HamCall) belongs in:
after the free offline FCC hit, before the CallParser prefix-DB fallback.

--- the source module contract ---------------------------------------------

A source is a plain module. No classes, no registration call; it becomes a
source by being listed in `lookup.SOURCES`. It exposes:

  SOURCE   str   provenance stamped into the record's `source` field
  CACHED   bool  may the dispatcher persist this source's OK results?
                 True  -> lookup.put()s the record; later lookups of that
                          callsign are answered from the cache without the
                          chain running at all (so sources ABOVE this one in
                          SOURCES are skipped until the row expires).
                 False -> nothing is written; the source is re-run every time.
                 Offline sources are False: they are microseconds, so a cache
                 row buys no latency and only costs correctness.
  setup(app)                 called once at boot from lookup.setup(). Must
                             never raise — warn and mark itself unavailable
                             so the server still boots (see lookup_fcc).
  close(app)                 optional. Release handles at shutdown.
  lookup(app, callsign)      -> {status, payload, error}. May be `async def`;
                             the dispatcher awaits an awaitable result. On a
                             miss return STATUS_NOT_FOUND — that is how a
                             source declines a callsign it doesn't handle
                             (there is no routing predicate; every source
                             sees every callsign). On breakage return
                             STATUS_ERROR: the chain keeps going but
                             remembers the first error, so a broken dataset
                             surfaces as a 502 rather than vanishing into a
                             "not found".
"""
import lookup_cache

SOURCE = "blank"

# True purely as the worked example of a caching source — the write path
# never actually fires, because lookup() below never returns an OK.
CACHED = True


# Nothing to open. Present so the chain can call setup() uniformly.
def setup(app):
    pass


# Always a miss: the chain falls through to the next source.
# The empty error string is the "I have nothing to say about this callsign"
# shape — an error string here would be remembered as the chain's first
# error and turn every unresolved lookup into a 502.
def lookup(app, callsign):
    return {
        "status": lookup_cache.STATUS_NOT_FOUND,
        "payload": {},
        "error": "",
    }
