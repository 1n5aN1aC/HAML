"""Callsign-lookup dispatcher: an ordered chain of interchangeable sources.

Holds the coalescing machinery (one asyncio task per active lookup, shared
future for concurrent callers), the source chain itself, and the cache
writes. Sources know nothing about each other, about the cache, or about
their position in the chain.

To add a source: write a module following the contract documented in
`lookup_blank`, then add it to `SOURCES` below. Nothing else changes —
`main.build_app` calls `lookup.setup(app)` once and the chain drives every
source's setup/teardown.

What lives here vs in a source module:
  here:  normalize_callsign, SOURCES, the fall-through rules, cache writes,
         inflight futures, _drive, schedule, setup, close
  there: the actual data access (sqlite / prefix DB / HTTP), its own rate
         gates and sessions, and the raw -> canonical mapping.

`api_rest.post_lookup` reads the cache, calls `schedule()` on a miss, and
post-processes the record on the way out; the chain is invisible above
that line.
"""
import asyncio
import inspect

import lookup_blank
import lookup_cache
import lookup_callparser
import lookup_fcc


# Normalize a raw callsign into the cache key + the value we send upstream.
# Uppercased and stripped of the suffixes the dispatcher treats as
# non-DXCC-meaningful: /P, /M, /MM, /QRP, /ANT, plus the trailing /
# that the formatter may leave behind. The list is local (`_CALLSIGN_SUFFIXES`
# below); CallParser keeps its own richer formatter inside callparser.py,
# but stripping these suffixes here keeps the cache key + every source
# consistent (a /MM-stripped key won't match a /MM-stripped CP result, etc.).
_CALLSIGN_SUFFIXES = ("/P", "/M", "/MM", "/QRP", "/ANT")
def normalize_callsign(raw):
    """Returns the normalized form, or '' when nothing usable remains."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip().upper().rstrip("/")
    for suffix in _CALLSIGN_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.strip().rstrip("/")
    return s

# --- the source chain ------------------------------------------------------
# Order is priority: the first source to return OK wins and the rest never
# run. Today:
#   fcc         offline US licensee DB.   CACHED=False — microseconds, and a
#               cache row would only let a stale answer outrank the DB.
#   blank       always misses.            CACHED=True  — the worked example,
#               and the slot an online provider (QRZ/HamQTH) drops into:
#               after the free US hit, before the prefix-DB fallback.
#   callparser  offline prefix DB.        CACHED=False — DXCC-level answers
#               for everything else; in-memory, so nothing to cache.
SOURCES = (lookup_fcc, lookup_blank, lookup_callparser)


# Run one source, normalizing both async results and exceptions into the
# {status, payload, error} shape. A source that blows up must not take the
# chain down — it presents as ERROR, the chain continues, and the error is
# still visible to the client if nothing below it resolves.
async def _run_source(source, app, callsign):
    try:
        result = source.lookup(app, callsign)
        # Sources may be sync (offline: microseconds) or `async def` (an online provider owning its own HTTP session).
        # The chain doesn't care which; it awaits whatever is awaitable.
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as exc:
        return {
            "status": lookup_cache.STATUS_ERROR,
            "payload": {},
            # A module broken enough to raise may never have bound itself,
            # an AttributeError here would take down the chain this handler exists to protect.
            "error": f"{getattr(source, 'SOURCE', source.__name__)}: {type(exc).__name__}: {exc}",
        }

# Walk the chain. First OK wins; a miss or an error falls through to the
# next source.
#
# Error handling is the subtle part: an erroring source must not abort the
# chain (a server with no FCC dataset should still answer DX calls from the
# prefix DB), but it must not vanish either (a missing dataset that silently
# reads as "callsign not found" is a support nightmare). So we remember the
# FIRST error and return it only if nothing below resolves — which is exactly
# the 502-vs-404 split the client already sees:
#   any source OK                     -> 200
#   all miss, none errored            -> 404
#   all miss, at least one errored    -> 502 with the first error's message
def _cache_write(app, callsign, source, result):
    """Persist an OK result when its source is a caching one.

    Sources never touch the cache themselves; this is the only writer.
    `dirty` is not plumbed through yet — no shipped source is cacheable, so
    every write would be clean anyway. When the first real caching source
    lands, add `dirty` to the source result shape (both offline adapters
    already compute `bad_fields`) and pass it here so a half-coerced record
    gets the 15-minute TTL instead of 365 days.
    """
    if not getattr(source, "CACHED", False):
        return
    lookup_cache.put(
        app["lookup_cache"],
        callsign,
        lookup_cache.STATUS_OK,
        result["payload"],
        source=source.SOURCE,
    )

async def _run_lookup(app, callsign):
    first_error = None
    for source in SOURCES:
        result = await _run_source(source, app, callsign)
        status = result["status"]
        if status == lookup_cache.STATUS_OK:
            _cache_write(app, callsign, source, result)
            return result
        if status == lookup_cache.STATUS_ERROR and first_error is None:
            first_error = result
    if first_error is not None:
        return first_error
    return {
        "status": lookup_cache.STATUS_NOT_FOUND,
        "payload": {},
        "error": "callsign not found",
    }

# --- coalescing futures ----------------------------------------------------
# One asyncio.Future per active lookup. The first POST for a callsign creates the future
# Additional POSTs for the same callsign reuse the same future.
# When the task finishes it resolves the future so every waiter gets the result.
def _get_or_create_future(app, callsign):
    inflight = app["inflight_lookups"]
    existing = inflight.get(callsign)
    if existing is not None and not existing.done():
        return existing, False
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    inflight[callsign] = future

    def _on_done(fut):
        # Remove ourselves from inflight once every waiter has been notified.
        inflight.pop(callsign, None)

    future.add_done_callback(_on_done)
    return future, True

# Drive the chain; resolve the future with its result. _run_source already
# wraps every source call, so a propagated exception is impossible in normal
# flow — this is a final safety net: a programming error in the dispatcher
# itself resolves the future with an ERROR row rather than letting waiters
# hang until the long-poll ceiling or surface a 500 to the client.
async def _drive(app, callsign, future):
    try:
        result = await _run_lookup(app, callsign)
    except Exception as exc:
        # Programming error or unexpected runtime fault in the chain.
        # The handler awaits the future and reads result["status"];
        # We therefore resolve with a STATUS_ERROR row so the handler returns
        # 502 (matching every other "lookup blew up" path) instead of surfacing the raw exception as a 500.
        result = {
            "status": lookup_cache.STATUS_ERROR,
            "payload": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not future.done():
        future.set_result(result)

# Public: schedule a lookup and return the future to await.
def schedule(app, callsign):
    future, is_new = _get_or_create_future(app, callsign)
    if is_new:
        loop = asyncio.get_running_loop()
        loop.create_task(_drive(app, callsign, future))
    return future

# setup(): the inflight dict, plus every source in the chain. Sources are
# required not to raise from setup() — an unavailable dataset warns and
# marks itself unavailable so the server still boots.
def setup(app):
    app["inflight_lookups"] = {}
    for source in SOURCES:
        source.setup(app)

# close(): release whatever the sources opened. `close` is optional on a
# source (most have nothing to release), so it's fetched defensively.
def close(app):
    for source in SOURCES:
        closer = getattr(source, "close", None)
        if closer is not None:
            closer(app)