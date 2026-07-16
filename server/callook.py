"""Callook upstream client + the coalescing/throttling around it.

This module is a pure adapter: it turns Callook's JSON shape into the
canonical record defined in `lookup_record`, and runs the coalescing and
rate-limiting that the /api/lookup handler relies on.

One asyncio task per active lookup:
The first POST handler for a callsign creates an asyncio.Future, awaits it, and runs the actual Callook hit.
Concurrent POSTs for the same callsign reuse the same Future.
When the task finishes, it writes a cache row and resolves the Future so every waiter gets the result.

Rate-limit gate: a simple asyncio.Lock + last-request timestamp holds us under Callook's published 1 req/s.
We acquire the lock, sleep until the gate opens, fire the request, release.
Two concurrent POSTs for different callsigns serialize at the gate — fine, it's only 1 second.

Supersession: when the queried callsign refers to a *previous* license
(e.g. KG7WKU now returns K1MI's record), the ok row is cached under the
returned callsign (warming future direct lookups for it), and the
queried key gets a standard not_found row (30-day TTL). The client
receives 404 — a previous call is not a currently-assigned callsign.
"""
import asyncio
import json
import time
import aiohttp
import lookup_cache
import lookup_record
import zones

SOURCE = "callook"

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

# Map a Callook JSON response into the canonical (provider-neutral) field names.
# Values are passed raw: typing/lowercasing happens in `lookup_record.coerce()`.
# Always returns a dict; never raises on missing fields (a near-complete row beats throwing the whole thing away).
def extract(callook_json):
    current = callook_json.get("current") or {}
    previous = callook_json.get("previous") or {}
    trustee = callook_json.get("trustee") or {}
    address = callook_json.get("address") or {}
    location = callook_json.get("location") or {}
    other = callook_json.get("otherInfo") or {}
    return {
        "callsign": current.get("callsign", ""),
        "name": callook_json.get("name", ""),
        "license_type": callook_json.get("type", ""),
        "license_class": current.get("operClass", ""),
        "previous_callsign": previous.get("callsign", ""),
        "previous_license_class": previous.get("operClass", ""),
        "trustee_callsign": trustee.get("callsign", ""),
        "trustee_name": trustee.get("name", ""),
        "address_line1": address.get("line1", ""),
        "address_line2": address.get("line2", ""),
        "address_attn": address.get("attn", ""),
        "latitude": location.get("latitude", ""),
        "longitude": location.get("longitude", ""),
        "gridsquare": location.get("gridsquare", ""),
        "frn": other.get("frn", ""),
        "grant_date": other.get("grantDate", ""),
        "expiry_date": other.get("expiryDate", ""),
        "last_action_date": other.get("lastActionDate", ""),
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

# Hit the Callook API and return the coerced canonical record plus a
# dirty flag. The dirty flag tells the cache layer to use a shortened
# TTL when the record contains fields that couldn't be coerced.
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
        data = json.loads(body)
    except ValueError as exc:
        raise CallookError("non-JSON response") from exc

    upstream_status = data.get("status")
    if upstream_status == "INVALID":
        return lookup_cache.STATUS_NOT_FOUND, {}, False
    if upstream_status != "VALID":
        raise CallookError(f"unexpected status: {upstream_status!r}")

    record, bad_fields = lookup_record.coerce(extract(data))
    # Derive CQ + ITU zones from the coordinates whenever we have them.
    # `zones.derive()` is total (never raises, returns None on bad input or when no polygon contains the point),
    # so this is safe to call blindly; we only assign over a still-null field so a direct value from a future provider would still win.
    if record.get("latitude") is not None and record.get("longitude") is not None:
        derived = zones.derive(record["latitude"], record["longitude"])
        if record.get("itu_zone") is None:
            record["itu_zone"] = derived["itu_zone"]
        if record.get("cq_zone") is None:
            record["cq_zone"] = derived["cq_zone"]
    dirty = bool(bad_fields)
    if dirty:
        print(
            f"warning: callook record for {callsign} has dirty fields: "
            f"{', '.join(bad_fields)}"
        )
    return lookup_cache.STATUS_OK, record, dirty

# Run a single lookup asynchronously.
async def _run_lookup(app, callsign):
    # _hit_callook only translates known transport/HTTP failures into CallookError;
    # Anything else (e.g. AttributeError on a malformed upstream payload) would otherwise escape as a 500.
    # Catch broadly and persist a STATUS_ERROR row so a retry is bounded by TTL_ERROR.
    try:
        status, payload, dirty = await _hit_callook(app, callsign)
    except CallookError as exc:
        status = lookup_cache.STATUS_ERROR
        payload = {}
        error = str(exc)
        dirty = False
    except Exception as exc:
        status = lookup_cache.STATUS_ERROR
        payload = {}
        error = f"{type(exc).__name__}: {exc}"
        dirty = False

    # Supersession: when the queried key is a previous call and Callook
    # returned the current license, cache the ok row under the returned
    # callsign (warming future direct lookups) and fall through to a
    # not_found result for the queried key. The 404 path below will
    # write that not_found row.
    superseded = False
    if status == lookup_cache.STATUS_OK and payload.get("callsign"):
        returned = normalize_callsign(payload["callsign"])
        if returned and returned != callsign:
            superseded = True

    # A failed cache write (sqlite locked, disk full) must not poison the
    # shared future — waiters still get the result; the next request for
    # this callsign simply re-hits Callook.
    cache = app["lookup_cache"]
    response_payload = payload
    try:
        if superseded:
            # Warm the cache for the returned callsign with the full record.
            lookup_cache.put(
                cache, payload["callsign"],
                lookup_cache.STATUS_OK, payload,
                source=SOURCE, dirty=dirty,
            )
            # Fall through to not_found for the queried key — write that
            # row last so it carries the actual result the client sees.
            lookup_cache.put(
                cache, callsign,
                lookup_cache.STATUS_NOT_FOUND, {},
                error="callsign not found",
            )
        else:
            response_payload = lookup_cache.put(
                cache, callsign, status, payload,
                error=error if status == lookup_cache.STATUS_ERROR else "",
                source=SOURCE if status == lookup_cache.STATUS_OK else "",
                dirty=dirty,
            )
    except Exception as exc:
        print(f"warning: lookup cache write failed for {callsign}: {exc}")

    if superseded:
        return {
            "status": lookup_cache.STATUS_NOT_FOUND,
            "payload": {},
            "error": "callsign not found",
        }
    return {
        "status": status,
        "payload": response_payload,
        "error": error if status == lookup_cache.STATUS_ERROR else "",
    }

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