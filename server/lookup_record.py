"""Canonical callsign-lookup record: strict, provider-neutral storage shape.

One home for the field set, the per-field coercers, and the timestamp format that the cache layer writes.
Providers (Callook today, QRZ/HamQTH tomorrow) adapt their raw JSON into the keys defined in `FIELDS`;
`coerce()` is the single place that turns whatever-the-upstream-sent into our vocabulary:

  - empty string  -> None
  - whitespace    -> stripped
  - enums         -> lowercased passthrough (no closed set yet)
  - lat/lon       -> float (None on parse failure -> dirty)
  - dates         -> YYYY-MM-DD ISO 8601 (None on parse failure -> dirty)
  - fetched_at    -> ISO 8601 UTC with milliseconds

`coerce()` never raises.
A present-but-uncoercible value is reported in `bad_fields` so the cache layer can flag
the row as dirty and shorten its TTL: that's the contract the caller relies on.
"""
from datetime import datetime, timezone


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
    "latitude",
    "longitude",
    "gridsquare",
    "frn",
    "grant_date",
    "expiry_date",
    "last_action_date",
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
    """MM/DD/YYYY -> YYYY-MM-DD. None on parse failure (dirty)."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


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
    "latitude": _coerce_float,
    "longitude": _coerce_float,
    "gridsquare": _coerce_str,
    "frn": _coerce_str,
    "grant_date": _coerce_iso_date,
    "expiry_date": _coerce_iso_date,
    "last_action_date": _coerce_iso_date,
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
