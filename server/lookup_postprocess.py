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
    lat, lon = record.get("latitude"), record.get("longitude")
    distance = None
    if lat is not None and lon is not None:
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

# Entry-form location fields that can trigger an override, paired with coercers
# so typed and lookup values are compared in canonical form.
_OVERRIDE_FIELDS = {
    "state": lookup_record._coerce_state,
    "section": lookup_record._coerce_upper,
    "gridsquare": lookup_record._coerce_gridsquare,
    "latitude": lookup_record._coerce_float,
    "longitude": lookup_record._coerce_float,
}

# Canonical record in, response record out. Never mutates the input.
# `entry` contains raw operator-edited fields for request-time derivations.
def apply(app, record, entry=None):
    # Load the record
    out = dict(record)

    # Fill missing fields
    out = _fill_missing_zones(out)
    out = _fill_distance(app, out)

    # Check if the user provided any location information which might override the operator values
    # Start from most precise location towards least-precise so we hit the mutator that would be most precise.
    if entry:
        # User-provided coordinates differ from existing user record
        if "latitude" in entry and "longitude" in entry:
            typed_lat = _OVERRIDE_FIELDS["latitude"](entry["latitude"])
            typed_lon = _OVERRIDE_FIELDS["longitude"](entry["longitude"])
            if (typed_lat is not None and typed_lon is not None and (typed_lat != record.get("latitude") or typed_lon != record.get("longitude"))):
                out["latitude"] = typed_lat
                out["longitude"] = typed_lon
                # Override grid      x
                # Override state     <
                # Override section   <
                # override county    <
                # override country   <
                # override dxcc      <
                # override cq zone   <
                # override ITU zone  <
                # override distance  <
                return out
        # User gave us a POTA park, parse it- ()
        elif "their_park" in entry and bool(str(entry["their_park"] or "").strip()):
            # Parse what park they are in.
                #Add that to the returned record
                #Update the coordinates
            # Override grid
            # Override state
            # Override section
            # override county
            # override country
            # override dxcc
            # override cq zone
            # override ITU zone
            # override distance
            return out
        # User-provided section differs from existing user record
        elif "section" in entry:
            typed = _OVERRIDE_FIELDS["section"](entry["section"])
            if typed is not None and typed != record.get("section"):
                # Override grid
                # Override state
                # override county
                # override country
                # override dxcc
                # override cq zone
                # override ITU zone
                # override distance
                return out
        # User-provided State differs from existing user record
        elif "state" in entry:
            typed = _OVERRIDE_FIELDS["state"](entry["state"])
            if typed is not None and typed != record.get("state"):
                # Override grid
                # Override section (if possible)
                # override county
                # override country
                # override dxcc
                # override cq zone
                # override ITU zone
                # override distance
                return out
        # User-provided gridsquare differs from existing user record
        elif "gridsquare" in entry:
            typed = _OVERRIDE_FIELDS["gridsquare"](entry["gridsquare"])
            if typed is not None and typed != record.get("gridsquare"):
                # Override state
                # Override section
                # override county
                # override country
                # override dxcc
                # override cq zone
                # override ITU zone
                # override distance
                return out

    # Calculate distance from him to us.
    out = _fill_distance(app, out)

    # Return finalized status
    return out
