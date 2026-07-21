"""Post-processing: the last stage a lookup record passes through.

`apply()` runs on every OK response — fresh chain results AND cache hits
alike — after the cache has been read or written. That ordering is the
point:

  - the cache stores what a source actually said (source truth), so a row
    stays meaningful no matter how derivation logic changes;
  - anything derived here takes effect immediately, with no cache to clear;
  - request-relative values (distance depends on the ACTIVE event's
    operating position) never get frozen into a row that outlives the event.

Input is the canonical record (`lookup_record.FIELDS`). Output is the wire
shape: those fields, possibly filled in further, plus request-time extras
that are deliberately not part of the storage contract (today: `distance`).
The input record is never mutated.

This is where the location-derivation work in TODO.md belongs — deriving a
location from grid/country, overriding one from state or a POTA park —
because it applies to every source at once instead of being reimplemented
per adapter.
"""
import math

import lookup_zones

# Mean Earth radius in kilometers.
_EARTH_RADIUS_KM = 6371.0


def _fill_zones(record):
    """Derive CQ + ITU zones from the record's coordinates.

    Only-fill-if-null: a source that already knows its zones wins, and wins
    before any work happens — a record carrying both zones returns untouched
    rather than deriving values it would discard. CallParser reads them
    straight out of the prefix DB, which is authoritative for a DXCC entity;
    the polygons here are a fallback for records (FCC rows) that carry
    coordinates and nothing else. `lookup_zones.derive` never raises and
    returns None for a point no polygon covers.
    """
    if record.get("cq_zone") is not None and record.get("itu_zone") is not None:
        return record
    if record.get("latitude") is None or record.get("longitude") is None:
        return record
    derived = lookup_zones.derive(record["latitude"], record["longitude"])
    if record.get("itu_zone") is None:
        record["itu_zone"] = derived["itu_zone"]
    if record.get("cq_zone") is None:
        record["cq_zone"] = derived["cq_zone"]
    return record


def _fill_distance(app, record):
    """Haversine kilometers from the active event's operating position
    (config.location) to the record's coordinates, floored to a whole
    number. None when either end is missing.
    """
    event = app.get("event") or {}
    loc = (event.get("config") or {}).get("location")
    lat, lon = record.get("latitude"), record.get("longitude")
    distance = None
    if loc and lat is not None and lon is not None:
        phi1 = math.radians(loc["latitude"])
        phi2 = math.radians(lat)
        d_phi = math.radians(lat - loc["latitude"])
        d_lam = math.radians(lon - loc["longitude"])
        a = (math.sin(d_phi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        distance = math.floor(_EARTH_RADIUS_KM * c)
    record["distance"] = distance
    return record


def apply(app, record):
    """Canonical record in, response record out. Never mutates the input."""
    out = dict(record)
    out = _fill_zones(out)
    out = _fill_distance(app, out)
    return out
