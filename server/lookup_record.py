"""Canonical callsign-lookup record: strict, provider-neutral storage shape.

One home for the field set, the per-field coercers, and the timestamp format that the cache layer writes.
Providers (FCC today, QRZ/HamQTH tomorrow) adapt their raw payload into the keys defined in `FIELDS`;
`coerce()` is the single place that turns whatever-the-upstream-sent into our vocabulary:

  - empty string  -> None
  - whitespace    -> stripped
  - enums         -> lowercased passthrough (no closed set yet)
  - state         -> USPS 2-letter code, accepting spelled-out names (None on unknown -> dirty)
  - lat/lon       -> float (None on parse failure -> dirty)
  - zones         -> int within an allowed range (None on parse failure -> dirty)
  - dates         -> YYYY-MM-DD ISO 8601 (None on parse failure -> dirty)
  - fetched_at    -> ISO 8601 UTC with milliseconds

`coerce()` never raises.
A present-but-uncoercible value is reported in `bad_fields` so the cache layer can flag
the row as dirty and shorten its TTL: that's the contract the caller relies on.
"""
from datetime import datetime, timezone
import re


# --- field set -------------------------------------------------------------

# The cache layer MUST be able to persist the record by JSON-encoding it,
# and the client MUST be able to consume it without validating. Both rely
# on the record containing exactly these keys, all of them, every time.
FIELDS = (
    "callsign",
    "source",
    "fetched_at",
    "name",
    "license_type",
    "license_class",
    "previous_callsign",
    "previous_license_class",
    "trustee_callsign",
    "trustee_name",
    "address_line1",
    "address_line2",
    "address_attn",
    "state",
    "county",
    "country",
    "continent",
    "latitude",
    "longitude",
    "gridsquare",
    "itu_zone",
    "cq_zone",
    "frn",
    "grant_date",
    "expiry_date",
)


# --- helpers ---------------------------------------------------------------

# ISO 8601 UTC with milliseconds, matching the rest of the server's timestamps.
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _coerce_str(value):
    """Strip a string. None / non-string / empty-after-strip -> None."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s if s else None


# Maidenhead 4-char field grid: two letters in [A-R] followed by two digits.
# Mirrors `BUILTINS.gridsquare.validation` client-side.
# Compiled once at module scope so the regex isn't rebuilt on every coerce() call.
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")


def _coerce_gridsquare(value):
    """Maidenhead grid truncated and validated to the 4-char field grid.

    The entry field accepts exactly 4 chars (BUILTINS.gridsquare.max_length,
    pattern `[A-R]{2}\\d{2}`). Longer grids exist for VHF/UHF callers but
    neither the field nor the cache can carry them, so we keep only the
    first 4 chars here, uppercase, and then validate the Maidenhead pattern;
    anything that doesn't match is treated as a present-but-uncoercible
    value (returned as None so coerce() flags the row dirty, the same as an
    unparseable date or latitude).

    Net effect: the cache stores — and every cache hit returns — an
    uppercase, pattern-valid 4-char grid or null; the client only null-checks.
    """
    s = _coerce_str(value)
    if s is None:
        return None
    g = s[:4].upper()
    return g if _GRID_RE.match(g) else None


# USPS full name -> two-letter code. Single source of truth: the valid-code
# set below is derived from these values, so adding an entry here makes both
# the spelled-out and two-letter forms coerce.
_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}
_STATE_CODES = frozenset(_STATE_NAMES.values()) # two-letter codes, derived from the name map above.


def _coerce_state(value):
    """US state as the USPS two-letter code.

    Accepts a two-letter code (any case) or a spelled-out name; always
    returns the uppercase code. Anything else — a code or name we don't
    recognize — is present-but-uncoercible (None -> dirty), same as a bad
    date or latitude.
    """
    s = _coerce_str(value)
    if s is None:
        return None
    u = s.upper()
    if len(u) == 2:
        return u if u in _STATE_CODES else None
    # Collapse internal whitespace so "NEW  YORK" still maps.
    return _STATE_NAMES.get(" ".join(u.split()))


def _coerce_lower(value):
    """Lowercase enum passthrough. None / non-string / empty -> None."""
    s = _coerce_str(value)
    return s.lower() if s is not None else None


def _coerce_float(value):
    """float, accepting ints and numeric strings. None on parse failure."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_iso_date(value):
    """YYYY-MM-DD passthrough; MM/DD/YYYY -> YYYY-MM-DD. None on parse failure (dirty).

    The FCC ULS dataset stores dates in ISO form already; Callook used
    MM/DD/YYYY. Accept both so adapters don't have to pre-normalize and
    a row that came from either upstream coerces cleanly.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # ISO 8601 date (YYYY-MM-DD).
    #   Accept it as-is after a length+shape check, so we don't let an unparseable string round-trip through
    #   datetime.fromisoformat and raise — strptime is the strict path.
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _coerce_zone(lo, hi):
    """Factory: int within [lo, hi], accepting ints, integer-valued floats,
    and numeric strings.

    Used by `itu_zone` (1..90) and `cq_zone` (1..40). Decimal zones aren't
    a thing in either system, so we reject fractional values and out-of-
    range integers as dirty. Numeric strings with leading zeros ('06')
    coerce fine — the contact entry form strips the padding on its side,
    but the canonical record prefers the canonical integer form.

    A None / non-numeric / out-of-range / fractional value returns None so
    `coerce()` can append it to bad_fields and the cache will use the
    shorter "dirty" TTL. Boolean values are rejected explicitly (Python's
    bool is an int subclass, so without the guard `True` would coerce to 1).
    """
    def coercer(value):
        if isinstance(value, bool):
            return None
        if value is None:
            return None
        if isinstance(value, (int, float)):
            f = float(value)
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            try:
                f = float(s)
            except ValueError:
                return None
        else:
            return None
        # A "zone" is inherently integer-valued; refuse fractional floats.
        if not f.is_integer():
            return None
        n = int(f)
        return n if lo <= n <= hi else None
    return coercer


# Per-field coercer map. A coercer returns None on "missing" or on a value
# that cannot be parsed; the caller distinguishes the two via the missing-
# value check (the input did not contain the key, or the value was empty).
_COERCERS = {
    "callsign": _coerce_str,
    "source": _coerce_str,
    "fetched_at": _coerce_str,
    "name": _coerce_str,
    "license_type": _coerce_lower,
    "license_class": _coerce_lower,
    "previous_callsign": _coerce_str,
    "previous_license_class": _coerce_lower,
    "trustee_callsign": _coerce_str,
    "trustee_name": _coerce_str,
    "address_line1": _coerce_str,
    "address_line2": _coerce_str,
    "address_attn": _coerce_str,
    "state": _coerce_state,
    "county": _coerce_str,
    "country": _coerce_str,
    "continent": _coerce_str,
    "latitude": _coerce_float,
    "longitude": _coerce_float,
    "gridsquare": _coerce_gridsquare,
    "itu_zone": _coerce_zone(1, 90),
    "cq_zone": _coerce_zone(1, 40),
    "frn": _coerce_str,
    "grant_date": _coerce_iso_date,
    "expiry_date": _coerce_iso_date,
}


# --- public API ------------------------------------------------------------

def coerce(raw):
    """Build a canonical record from `raw` (a provider-specific dict).

    Returns (record, bad_fields) where:
      - record has exactly the keys in FIELDS, all populated (None when absent)
      - bad_fields is the list of field names whose input was present but
        could not be coerced — the row should be flagged dirty and given
        a shortened TTL.

    Never raises. Unknown keys in `raw` are dropped. A field is dirty only
    when the input had a value that wouldn't coerce; missing/empty values
    produce a clean None (sparse data is not an error).
    """
    raw = raw if isinstance(raw, dict) else {}
    record = {}
    bad_fields = []
    for field in FIELDS:
        if field not in raw:
            record[field] = None
            continue
        value = raw[field]
        # Treat empty strings as "absent" — sparse data, not a coercion failure.
        is_empty = value is None or (isinstance(value, str) and not value.strip())
        if is_empty:
            record[field] = None
            continue
        coercer = _COERCERS[field]
        coerced = coercer(value)
        if coerced is None:
            # Present but uncoercible (e.g. latitude="abc", bad date) -> dirty.
            record[field] = None
            bad_fields.append(field)
        else:
            record[field] = coerced
    return record, bad_fields
