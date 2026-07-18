"""CallParser adapter: prefix-DB fallback for callsign lookup.

Pure adapter — no HTTP, no async, no I/O beyond the in-memory Prefix.lst
parse that callparser.init() does at setup. On miss, hands back
STATUS_NOT_FOUND; on load failure, setup() flips a flag and lookup()
becomes a permanent miss (the FCC hop still decides the response).

The chain seam is left visible in `lookup._run_lookup`: future online
providers (QRZ, HamQTH) append there, own their own HTTP sessions and
gates, and write results into the cache. This module writes nothing —
CallParser results are NEVER cached. The reasoning: the prefix DB is
fast (microseconds, in-memory), so a cache row buys nothing on latency
and would only shorten the hit path of a never-firing cache lookup.
A future online provider between FCC and CallParser is the layer that
writes lookup_cache; this rule (no writes here) is pinned so the
seam stays honest when the online hop lands.
"""
import callparser
import lookup_cache
import lookup_record

SOURCE = "callparser"


# setup(): called from main.build_app.
# Missing/unopenable Prefix.lst -> warn, mark not ready.
# We never raise; the server must boot so the admin endpoints still work
# and the FCC hop keeps answering US calls on a server with no DX data.
def setup(app):
    path = app["cfg"]["prefix_lst_path"]
    try:
        callparser.init(str(path))
        # Touch is_loaded() so a malformed file that didn't raise during
        # parse is still flagged before the first lookup fires.
        if not callparser.is_loaded():
            raise RuntimeError("callparser.init() returned without loading")
        app["callparser_ready"] = True
    except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
        print(
            f"warning: CallParser prefix list unavailable at {path} ({exc}); "
            "DX callsign lookups will fall through to a 404"
        )
        app["callparser_ready"] = False

# --- hit -> canonical mapping ---------------------------------------------
# Prefix.lst zone strings ("5", "08") coerce cleanly through _coerce_zone;
# adif through _coerce_zone(1,999). Province/city are dropped on purpose:
# the `state` coercer is US-only, so mapping them would either dirty the
# record or lie. The client only null-checks.
def _build_record(callsign, hit):
    raw = {
        "callsign": callsign,
        "country": hit["territory"] or "",
        "continent": hit["continent"] or "",
        "cq_zone": hit["cq"] or "",
        "itu_zone": hit["itu"] or "",
        "dxcc": hit["adif"] or "",
    }
    coords = callparser.coords(hit)
    if coords is not None:
        lat, lon = coords
        raw["latitude"] = lat
        raw["longitude"] = lon
    return raw

# lookup(): one in-memory call; sync because the work is microseconds.
# Returns the {status, payload, error} shape the chain seam expects.
def lookup(app, callsign):
    if not app.get("callparser_ready"):
        # Chain treats this as "this provider has nothing" so the caller's
        # prior FCC status decides the response. See lookup._run_lookup.
        return {
            "status": lookup_cache.STATUS_NOT_FOUND,
            "payload": {},
            "error": "",
        }

    try:
        hit = callparser.lookup(callsign)
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        # Defensive: a malformed call that gets past _format_call's None
        # returns shouldn't take the whole chain down. Mirror the FCC
        # adapter's behavior: present as ERROR so a missing-DB chain
        # stays visible (FCC error + CP error = 502).
        return {
            "status": lookup_cache.STATUS_ERROR,
            "payload": {},
            "error": f"callparser error: {type(exc).__name__}: {exc}",
        }

    if hit is None:
        return {
            "status": lookup_cache.STATUS_NOT_FOUND,
            "payload": {},
            "error": "callsign not found",
        }

    raw = _build_record(callsign, hit)
    record, bad_fields = lookup_record.coerce(raw)

    # Stamp source + fetched_at here, since the cache layer is bypassed
    # (CallParser results are never cached — see module docstring).
    record["source"] = SOURCE
    record["fetched_at"] = lookup_record.now_iso()

    dirty = bool(bad_fields)
    if dirty:
        print(
            f"warning: callparser record for {callsign} has dirty fields: "
            f"{', '.join(bad_fields)}"
        )
    return {
        "status": lookup_cache.STATUS_OK,
        "payload": record,
        "error": "",
    }