"""Provider-neutral callsign-lookup dispatcher.

Holds the coalescing machinery (one asyncio task per active lookup, shared
future for concurrent callers) and the provider chain seam. Today the chain
is a single hop — `fcc.lookup()` — but future online providers (QRZ,
HamQTH, …) append below, own their own HTTP sessions / rate gates, and
write results into `lookup_cache` themselves.

What lives here vs in a provider module:
  here: normalize_callsign, inflight futures, _drive, schedule, setup
  there: HTTP / rate-limit / parsing / aiohttp.ClientSession, a per-provider
         `lookup(app, callsign) -> {status, payload, error}` function.

`api_rest.post_lookup` calls `schedule()` and consumes the result; the
provider chain is invisible above this line.
"""
import asyncio

import fcc
import lookup_cache


# Normalize a raw callsign into the cache key + the value we send upstream.
# Uppercased and stripped of the suffixes the dispatcher treats as
# non-DXCC-meaningful: /P, /M, /MM, /QRP, /ANT, plus the trailing /
# that the formatter may leave behind. The list is local (`_CALLSIGN_SUFFIXES`
# below); when a server-side CallParser fallback is added later, its strip-list
# must stay in lock-step with this one.
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


# --- provider chain --------------------------------------------------------
# Today: FCC offline, the only provider. Hits the local sqlite at the
# path the server was configured with; miss is a 404; missing DB is 502.
#
# To add an online provider (QRZ, HamQTH) for non-US calls:
#   1. append a call here after the FCC hop, gated on a miss/error
#   2. have it call lookup_cache.put() to warm the cache for next time
#   3. own its own aiohttp.ClientSession, rate gate, and HTTP error handling
# FCC is offline-primary: a miss is the truth, never a fallback to a paid
# upstream — saves both latency and money on the common US-call case.
def _run_lookup(app, callsign):
    # Wrap provider exceptions so the coalesced future still resolves with a result
    try:
        result = fcc.lookup(app, callsign)
    except Exception as exc:
        return {
            "status": lookup_cache.STATUS_ERROR,
            "payload": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    # The chain seam: when a future online provider is added, it slots in
    # here. e.g.:
    #   if result["status"] == lookup_cache.STATUS_NOT_FOUND:
    #       return qrz.lookup(app, callsign)
    return result

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

# Drive the chain; resolve the future with its result. The chain's own
# try/except makes a propagated exception impossible in normal flow, so
# this is a final safety net: a programming error in the dispatcher
# resolves the future with an ERROR row rather than letting waiters hang
# until the long-poll ceiling or surface a 500 to the client.
#
# Stays `async def` because `loop.create_task()` requires a coroutine,
# even though the current body is sync (FCC is microseconds). When a
# future online provider is appended, this body becomes an
# `await asyncio.gather(...)` over the sync/async results it returns —
# the handler doesn't change.
async def _drive(app, callsign, future):
    try:
        result = _run_lookup(app, callsign)
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

# setup(): inflight dict only.
def setup(app):
    app["inflight_lookups"] = {}