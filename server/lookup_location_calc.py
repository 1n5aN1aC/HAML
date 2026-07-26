"""Location-derived lookups: everything computed from a latitude/longitude.

Home for the derivations that turn a coordinate into some other geographic
fact about it — CQ and ITU zone numbers today, more to follow. The module is
split so a new derivation only has to add its own data + entry point:

  - shared coordinate handling (`_valid_coord`),
  - shared GeoJSON parsing and point-in-polygon machinery, and
  - one section per derivation, holding its dataset and its public functions.

A derivation exposes two levels: a `derive_*(lat, lon)` that answers for a
bare coordinate, and `recalculate_*(record)` wrappers that write the answer
straight into a canonical lookup record (`lookup_record.FIELDS`). The
wrappers live here rather than in the caller so the field name, the
coordinate source, and the derivation that fills it stay in one place.

Every dataset is loaded lazily on first use of the derivation that needs it,
to keep cold-start cost out of the import path: modules that merely
`import lookup_location_calc` (e.g. an offline provider) don't pay for a JSON
parse until at least one lookup of that kind fires. A derivation that never
runs costs nothing.

Every public function here must never raise. Bad inputs (None, NaN,
out-of-range, non-numeric) and points no dataset covers (open ocean, say)
both return None for the affected field, so callers can use the result
without validating it.

Coordinate convention:
  - Callers pass (lat, lon). Standard geographic order.
  - GeoJSON stores coordinates as (lon, lat) per the spec.
  - We swap on read so the rest of this module reasons in (lat, lon).
  - Vendored polygons that cross the antimeridian are stored with unwrapped
    longitudes running past ±180 (e.g. -200 means 160°E); `_lookup_region()`
    handles this by also testing the point at lon±360.
"""
import json
import math
from pathlib import Path

# Vendored data location, relative to this file. Created at setup time.
_DATA_DIR = Path(__file__).parent / "datasets"


# --- shared coordinate handling --------------------------------------------

def _valid_coord(lat, lon):
    """(lat, lon) as floats, or None if the pair isn't a usable coordinate.

    One gate in front of every derivation, so they all reject the same
    things the same way: None, non-numeric, NaN, or outside the geographic
    range (lat [-90, 90], lon [-180, 180]). Out-of-range is rejected rather
    than clamped or wrapped — a coordinate that far off is bad input, and
    testing it anyway risks a misleading hit from a polygon whose bounding
    box happens to reach the edge.
    """
    try:
        if lat is None or lon is None:
            return None
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if math.isnan(lat_f) or math.isnan(lon_f):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    return lat_f, lon_f


# --- shared GeoJSON parsing -------------------------------------------------

def _swap(coords):
    """Convert GeoJSON [lon, lat] pairs to (lat, lon) tuples.

    Vendored data is strictly Polygon (verified at vendoring). MultiPolygon
    is supported defensively for what the next upstream revision might
    bring, but in practice never appears.
    """
    return [(lat, lon) for lon, lat in coords]

def _rings_of_feature(feature):
    """Flat list of rings (each a list of (lat, lon) tuples) for one feature."""
    geom = feature.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    out = []
    if gtype == "Polygon":
        for ring_coords in coords:
            out.append(_swap(ring_coords))
    elif gtype == "MultiPolygon":
        for polygon in coords:
            for ring_coords in polygon:
                out.append(_swap(ring_coords))
    return out

def _bbox_of_rings(rings):
    """Lat/lon bbox covering all rings of a feature (outer + holes)."""
    lats_min = lats_max = None
    lons_min = lons_max = None
    for ring in rings:
        for lat, lon in ring:
            if lats_min is None or lat < lats_min: lats_min = lat
            if lats_max is None or lat > lats_max: lats_max = lat
            if lons_min is None or lon < lons_min: lons_min = lon
            if lons_max is None or lon > lons_max: lons_max = lon
    return (lats_min, lats_max, lons_min, lons_max)

def _parse_geojson(path, value_key):
    """Read a FeatureCollection into a flat table of regions.

    Each region is:
      { "value": <property `value_key`, as int>,
        "bbox": (lat_min, lat_max, lon_min, lon_max),
        "rings": [ [ (lat, lon), ... ], ... ] }
    Ring[0] is the outer ring; ring[1:] are holes. Parity of containment
    across all rings gives the even-odd inside/outside test.

    `value_key` names the feature property carrying whatever the derivation
    wants back for a point inside that region — a zone number today.
    Malformed features (missing / non-integer value, no rings) are skipped so
    one bad feature doesn't poison the entire dataset.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    features = data.get("features") or []
    regions = []
    for feat in features:
        props = feat.get("properties") or {}
        try:
            value = int(props[value_key])
        except (KeyError, TypeError, ValueError):
            continue
        rings = _rings_of_feature(feat)
        if not rings:
            continue
        regions.append({
            "value": value,
            "bbox": _bbox_of_rings(rings),
            "rings": rings,
        })
    return regions


# --- shared point-in-polygon ------------------------------------------------

def _point_in_bbox(lat, lon, bbox):
    """False if (lat, lon) is clearly outside the feature's bbox."""
    lat_min, lat_max, lon_min, lon_max = bbox
    if lat < lat_min or lat > lat_max:
        return False
    if lon < lon_min or lon > lon_max:
        return False
    return True

def _point_in_ring(lat, lon, ring):
    """Even-odd ray-casting test: is the point inside this closed ring?

    Standard horizontal-ray test against the polygon's segments. Treats
    the ring as closed by walking j backward through i.
    """
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        # Edge crosses the horizontal ray at lat iff (yi > lat) != (yj > lat).
        if (yi > lat) != (yj > lat):
            # Linear interpolation of the edge at y=lat, then compare to lon.
            x_intersect = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if x_intersect > lon:
                inside = not inside
        j = i
    return inside

def _point_in_polygon(lat, lon, rings):
    """Point is inside a polygon iff it's inside an odd number of rings.

    Outermost ring is ring[0]; any hole rings toggle the parity.
    """
    parity = 0
    for ring in rings:
        if _point_in_ring(lat, lon, ring):
            parity += 1
    return (parity % 2) == 1

def _lookup_region(lat, lon, regions):
    """Value of the first region whose polygon contains (lat, lon), or None.

    Each region is bbox-prefiltered, so the per-point cost scales with the
    few regions that overlap the point, not the full feature count.

    Dateline-crossing polygons in the vendored data are stored unwrapped
    (longitudes run past ±180; -200 means 160°E). Longitude is periodic
    mod 360, so testing the point at lon±360 as well covers them exactly.
    The bbox prefilter rejects the shifted copies instantly for the ~all
    regions that don't wrap, so the extra cost is two tuple comparisons.
    """
    for region in regions:
        for lo in (lon, lon - 360.0, lon + 360.0):
            if not _point_in_bbox(lat, lo, region["bbox"]):
                continue
            if _point_in_polygon(lat, lo, region["rings"]):
                return region["value"]
    return None


# --- CQ / ITU zones ---------------------------------------------------------

# Lazily populated by _ensure_zones_loaded(); region tables per _parse_geojson.
_CQ_ZONES = None
_ITU_ZONES = None

def _ensure_zones_loaded():
    """Lazy-load both zone GeoJSON files on first use."""
    global _CQ_ZONES, _ITU_ZONES
    if _CQ_ZONES is None:
        _CQ_ZONES = _parse_geojson(_DATA_DIR / "mapregions_cqzones.geojson", "cq_zone_number")
    if _ITU_ZONES is None:
        _ITU_ZONES = _parse_geojson(_DATA_DIR / "mapregions_ituzones.geojson", "itu_zone_number")

def derive_zones(lat, lon):
    """{ 'cq_zone': int|None, 'itu_zone': int|None } for a coordinate.

    Never raises. A field is None when the coordinate isn't usable (see
    `_valid_coord`) or when no polygon in that dataset covers the point. The
    two zone systems are looked up independently, so one can resolve while
    the other doesn't.
    """
    coord = _valid_coord(lat, lon)
    if coord is None:
        return {"cq_zone": None, "itu_zone": None}
    lat_f, lon_f = coord

    _ensure_zones_loaded()
    return {
        "cq_zone": _lookup_region(lat_f, lon_f, _CQ_ZONES),
        "itu_zone": _lookup_region(lat_f, lon_f, _ITU_ZONES),
    }

# Set the record's CQ zone from its coordinates, overwriting any value.
# A record with no coordinates is returned untouched — there is nothing to
# derive from, so an existing source-supplied zone is left alone.
def recalculate_cq_zone(record):
    if record.get("latitude") is None or record.get("longitude") is None:
        return record
    record["cq_zone"] = derive_zones(
        record["latitude"], record["longitude"])["cq_zone"]
    return record

# Set the record's ITU zone from its coordinates, overwriting any value.
# Same no-coordinates rule as recalculate_cq_zone.
def recalculate_itu_zone(record):
    if record.get("latitude") is None or record.get("longitude") is None:
        return record
    record["itu_zone"] = derive_zones(
        record["latitude"], record["longitude"])["itu_zone"]
    return record
