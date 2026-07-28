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
that are deliberately not part of the storage contract (today: `distance`
and `pota_park`). Every extra is always present, null when it has nothing to
say, so the client reads them exactly like the canonical fields.
The input record is never mutated.

This is where the location-derivation work in TODO.md belongs — deriving a
location from grid/country, overriding one from state or a POTA park —
because it applies to every source at once instead of being reimplemented
per adapter.
"""
import math
import lookup_record
import lookup_location_calc

_EARTH_RADIUS_KM = 6371.0                                   # Mean Earth radius in kilometers.
_DEFAULT_LOCATION = {"latitude": 45.0, "longitude": -123.0} # Default location if the event does not provide one.

# Derive missing zones from the records coordinates. Only the fields that
# arrived empty are named, so a source-supplied zone (CallParser's prefix-DB
# zones, say) stays as it is and costs no query.
def _fill_missing_zones(record):
    missing = [f for f in ("itu_zone", "cq_zone") if record.get(f) is None]
    if missing:
        record = lookup_location_calc.recalculate(record, missing)
    return record

# Derive the distance in km from the active event's operating position (config.location)
def _fill_distance(app, record):
    event = app.get("event") or {}
    loc = (event.get("config") or {}).get("location") or _DEFAULT_LOCATION
    # Both ends through the same gate every other coordinate here passes: a missing,
    # NaN or out-of-range value answers None instead of raising out of apply().
    here = lookup_location_calc._valid_coord(loc.get("latitude"), loc.get("longitude"))
    there = lookup_location_calc._valid_coord(record.get("latitude"), record.get("longitude"))
    distance = None
    if here is not None and there is not None:
        (op_lat, op_lon), (lat, lon) = here, there
        phi1 = math.radians(op_lat)
        phi2 = math.radians(lat)
        d_phi = math.radians(lat - op_lat)
        d_lam = math.radians(lon - op_lon)
        a = (math.sin(d_phi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        distance = math.floor(_EARTH_RADIUS_KM * c)
    record["distance"] = distance
    return record

# Entry-form location fields that can trigger an override, paired with coercers
# so typed and lookup values are compared in canonical form.
_OVERRIDE_FIELDS = {
    "state": lookup_record._coerce_state,
    "section": lookup_record._coerce_upper,
    "gridsquare": lookup_record._coerce_gridsquare,
    "latitude": lookup_record._coerce_float,
    "longitude": lookup_record._coerce_float,
}

# An operator-edited field's coerced value, or None when this field has nothing to say:
# absent from `entry`, emptied, uncoercible, or the same as what the lookup already returned.
def _overridden(entry, record, field):
    typed = _OVERRIDE_FIELDS[field](entry.get(field))
    return typed if (typed is not None and typed != record.get(field)) else None

# Canonical record in, response record out. Never mutates the input.
# `entry` contains raw operator-edited fields for request-time derivations.
def apply(app, record, entry=None):
    out = dict(record)      # Load the record
    out["pota_park"] = None # Ensure wire shape always includes this field

    # Fill missing fields (only ITU & cq zones as of today)
    out = _fill_missing_zones(out)

    # Check if the user provided any location information which might override the operator values
    # Ordered by how reliably each names where someone is, not by how tight a box it draws:
    # a grid centre is tighter than a state anchor but can land across a state line, and a
    # wrong state is worse than a coarse one — so gridsquare is tried last, not first.
    if entry:
        typed_lat = _OVERRIDE_FIELDS["latitude"](entry.get("latitude"))
        typed_lon = _OVERRIDE_FIELDS["longitude"](entry.get("longitude"))
        coords = (typed_lat, typed_lon) if (
            typed_lat is not None and typed_lon is not None
            and (typed_lat != record.get("latitude")
                 or typed_lon != record.get("longitude"))) else None
        typed_section = _overridden(entry, record, "section")
        typed_state = _overridden(entry, record, "state")
        typed_grid = _overridden(entry, record, "gridsquare")

        if coords is not None:
            out["latitude"] = typed_lat
            out["longitude"] = typed_lon
            lookup_location_calc.recalculate(out)                                             # Coords given, trust them implicitly and update EVERYTHING
        elif lookup_location_calc.process_park(out, entry) is not None:
            lookup_location_calc.recalculate(out)                                             # Coords from POTA, we trust that implicitly and update EVERYTHING
        elif typed_section and lookup_location_calc.process_section(out, typed_section):
            lookup_location_calc.recalculate(out, ["state", "country", "dxcc", "cq_zone", "itu_zone"]) # Recalculate everything downstream of section
            lookup_record.blank(out, ["gridsquare", "county"])                                         # Remove inaccurate information
        elif typed_state and lookup_location_calc.process_state(out, typed_state):
            lookup_location_calc.recalculate(out, ["country", "dxcc", "cq_zone", "itu_zone"]) # Recalculate everything downstream of state
            lookup_record.blank(out, ["gridsquare", "county", "section"])                     # Remove inaccurate information
            lookup_location_calc.recalculate_section_from_state(out)                          # (try to) re-get section from state.  (blank for states >1 section)
        elif typed_grid and lookup_location_calc.process_gridsquare(out, typed_grid):
            lookup_location_calc.recalculate(out, ["country", "dxcc", "cq_zone", "itu_zone"]) # Recalculate everything downstream of gridsquare
            lookup_record.blank(out, ["county", "section", "state"])                          # Remove inaccurate information

    # Calculate distance from him to us.
    # This needs to be last, because other bits could have overridden the coordinates.
    out = _fill_distance(app, out)
    return out