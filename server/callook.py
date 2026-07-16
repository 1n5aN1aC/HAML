"""Callook upstream client + the coalescing/throttling around it.

One asyncio task per active lookup:
The first POST handler for a callsign creates an asyncio.Future, awaits it, and runs the actual Callook hit.
Concurrent POSTs for the same callsign reuse the same Future.
When the task finishes, it writes a cache row and resolves the Future so every waiter gets the result.

Rate-limit gate: a simple asyncio.Lock + last-request timestamp holds us under Callook's published 1 req/s.
We acquire the lock, sleep until the gate opens, fire the request, release.
Two concurrent POSTs for different callsigns serialize at the gate — fine, it's only 1 second.
"""
import asyncio
import time
import aiohttp
import lookup_cache

# Callook's URL pattern. Configurable per test setup via cfg.
DEFAULT_BASE_URL = "https://callook.info"
# Per-request timeout- Comfortably less than the long-poll ceiling.
REQUEST_TIMEOUT_S = 5
# Rate-limit gate: one request per second.
MIN_INTERVAL_S = 1.0

# Normalize a raw callsign into the cache key + the value we send upstream.
# Uppercased and stripped of the suffixes CallParser treats as
# non-DXCC-meaningful (Q7): /P, /M, /MM, /QRP, /ANT, plus the trailing /
# that the formatter may leave behind.
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

# Map a Callook JSON response into our normalized cache entry.
# Always returns a dict (never raises on missing fields — a partial result
# is more useful than throwing away a near-complete row).
def extract(callook_json):
    current = callook_json.get("current") or {}
    previous = callook_json.get("previous") or {}
    trustee = callook_json.get("trustee") or {}
    address = callook_json.get("address") or {}
    location = callook_json.get("location") or {}
    other = callook_json.get("otherInfo") or {}
    return {
        "callsign": current.get("callsign", ""),
        "status": callook_json.get("status", ""),
        "type": callook_json.get("type", ""),
        "OperatorClass": current.get("operClass", ""),
        "PreviousCallsign": previous.get("callsign", ""),
        "PreviousOperatorClass": previous.get("operClass", ""),
        "name": callook_json.get("name", ""),
        "TrusteeCallsign": trustee.get("callsign", ""),
        "TrusteeName": trustee.get("name", ""),
        "address": {
            "line1": address.get("line1", ""),
            "line2": address.get("line2", ""),
            "attn": address.get("attn", ""),
        },
        "location": {
            "latitude": location.get("latitude", ""),
            "longitude": location.get("longitude", ""),
            "gridsquare": location.get("gridsquare", ""),
        },
        "grantDate": other.get("grantDate", ""),
        "expiryDate": other.get("expiryDate", ""),
        "lastActionDate": other.get("lastActionDate", ""),
        "fetched_at": lookup_cache.now_iso(),
    }

# Outcome of one upstream hit, ready for the cache layer.
# Status maps to lookup_cache.STATUS_*;
# payload is the normalized dict (or empty on not_found/error);
# error is a human-readable message for status=error.
class CallookError(Exception):
    """Raised when we cannot reach Callook or its response is unusable."""

# Wait for the rate-limit gate, then GET.
async def _throttled_get(app, url):
    http = app["http"]
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    # aiohttp can raise aiohttp.ClientError, asyncio.TimeoutError, or
    # return a non-2xx response; the caller turns all of those into either
    # a cache row or an exception so the coalesced future resolves once.
    async with http.get(url, timeout=timeout) as resp:
        return resp.status, await resp.read()

# Hit the Callook API and return the normalized response.
async def _hit_callook(app, callsign):
    base_url = app["cfg"].get("callook_base_url", DEFAULT_BASE_URL)
    url = f"{base_url}/{callsign}/json"
    gate = app["callook_gate"]
    async with gate:
        # Prevent a burst of concurrent POSTs from exceeding the 1 req/s limit.
        last_at = app["callook_last_at"]
        now = time.monotonic()
        wait = MIN_INTERVAL_S - (now - last_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            status_code, body = await _throttled_get(app, url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            app["callook_last_at"] = time.monotonic()
            raise CallookError(str(exc)) from exc
        app["callook_last_at"] = time.monotonic()

    if status_code != 200:
        raise CallookError(f"HTTP {status_code}")

    try:
        data = __import__("json").loads(body)
    except ValueError as exc:
        raise CallookError("non-JSON response") from exc

    upstream_status = data.get("status")
    if upstream_status == "INVALID":
        return lookup_cache.STATUS_NOT_FOUND, {}, ""
    if upstream_status != "VALID":
        raise CallookError(f"unexpected status: {upstream_status!r}")

    payload = extract(data)
    payload["callsign"] = callsign
    return lookup_cache.STATUS_OK, payload, ""

# Run a single lookup asynchronously.
async def _run_lookup(app, callsign):
    # _hit_callook only translates known transport/HTTP failures into CallookError;
    # Anything else (e.g. AttributeError on a malformed upstream payload) would otherwise escape as a 500.
    # Catch broadly and persist a STATUS_ERROR row so a retry is bounded by TTL_ERROR.
    try:
        status, payload, error = await _hit_callook(app, callsign)
    except CallookError as exc:
        status = lookup_cache.STATUS_ERROR
        payload = {}
        error = str(exc)
    except Exception as exc:
        status = lookup_cache.STATUS_ERROR
        payload = {}
        error = f"{type(exc).__name__}: {exc}"
    # A failed cache write (sqlite locked, disk full) must not poison the
    # shared future — waiters still get the result; the next request for
    # this callsign simply re-hits Callook.
    try:
        lookup_cache.put(app["callook_cache"], callsign, status, payload, error)
    except Exception as exc:
        print(f"warning: callook cache write failed for {callsign}: {exc}")
    return {"status": status, "payload": payload, "error": error}

# Get an existing future or create a new one for a callsign.
def _get_or_create_future(app, callsign):
    """Return (future, is_new). Concurrent callers share the same future;
    the first one is responsible for actually scheduling the task."""
    inflight = app["inflight_lookups"]
    existing = inflight.get(callsign)
    if existing is not None and not existing.done():
        return existing, False
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    inflight[callsign] = future

    def _on_done(fut):
        # Remove ourselves from inflight once every waiter has been
        # notified. Exceptions are not propagated here — the Future itself
        # carries them; we just stop holding a reference.
        inflight.pop(callsign, None)

    future.add_done_callback(_on_done)
    return future, True

# Drive the actual lookup; resolve the future with its result.
async def _drive(app, callsign, future):
    try:
        result = await _run_lookup(app, callsign)
        if not future.done():
            future.set_result(result)
    except Exception as exc:
        if not future.done():
            future.set_exception(exc)

# Schedule a lookup for a callsign.
def schedule(app, callsign):
    future, is_new = _get_or_create_future(app, callsign)
    if is_new:
        loop = asyncio.get_running_loop()
        loop.create_task(_drive(app, callsign, future))
    return future

# Application setup
# aiohttp.ClientSession is created in on_startup (which runs inside the event loop)
async def _start_http(app):
    app["http"] = aiohttp.ClientSession()

# Application teardown
async def _close_http(app):
    http = app.get("http")
    if http is not None and not http.closed:
        await http.close()

# Application setup
def setup(app):
    app["callook_gate"] = asyncio.Lock()
    # Last-request monotonic timestamp; initialized in the past so the first
    # POST doesn't sleep a full second waiting on its own gate.
    app["callook_last_at"] = 0.0
    app["inflight_lookups"] = {}
    app.on_startup.append(_start_http)
    app.on_shutdown.append(_close_http)