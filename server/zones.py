"""CQ and ITU zone lookup from latitude/longitude.

Derives amateur radio zone numbers from coordinates using vendored GeoJSON
boundary data. Loaded lazily on first call to keep cold-start cost out of
the import path: modules that merely `import zones` (callook) don't pay for
the JSON parse until at least one lookup fires.

Point-in-polygon uses even-odd ray-casting on each polygon's outer ring plus
holes. Each feature is bbox-prefiltered, so the per-point cost scales with
the few zones that overlap the point, not the full feature count.

`derive()` never raises. Bad inputs (None, NaN, out-of-range, non-numeric)
and points over open ocean (or anywhere not covered by a zone polygon) both
return None for that field.

Coordinate convention:
  - Caller passes (lat, lon). Standard geographic order.
  - GeoJSON stores coordinates as (lon, lat) per the spec.
  - We swap on read so the rest of this module reasons in (lat, lon).
  - Vendored polygons that cross the antimeridian are stored with unwrapped
    longitudes running past ±180 (e.g. -200 means 160°E); _lookup() handles
    this by also testing the point at lon±360.
"""
import json
import math
from pathlib import Path

# Vendored data location, relative to this file. Created at setup time.
_DATA_DIR = Path(__file__).parent / "zonedata"

# Lazily populated by _ensure_loaded(). Each list is a flat table of zones:
#   { "number": int, "bbox": (lat_min, lat_max, lon_min, lon_max),
#     "rings": [ [ (lat, lon), ... ], ... ] }
# Ring[0] is the outer ring; ring[1:] are holes. Parity of containment
# across all rings gives the even-odd inside/outside test.
_CQ_ZONES = None
_ITU_ZONES = None


# --- parsing ---------------------------------------------------------------

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

def _parse_geojson(path, number_key):
    """Read a FeatureCollection and return the list of zone records.

    Malformed features (missing number, no rings) are skipped so one bad
    feature doesn't poison the entire dataset.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    features = data.get("features") or []
    zones = []
    for feat in features:
        props = feat.get("properties") or {}
        try:
            number = int(props[number_key])
        except (KeyError, TypeError, ValueError):
            continue
        rings = _rings_of_feature(feat)
        if not rings:
            continue
        zones.append({
            "number": number,
            "bbox": _bbox_of_rings(rings),
            "rings": rings,
        })
    return zones

def _ensure_loaded():
    """Lazy-load both GeoJSON files on first use."""
    global _CQ_ZONES, _ITU_ZONES
    if _CQ_ZONES is None:
        _CQ_ZONES = _parse_geojson(_DATA_DIR / "cqzones.geojson", "cq_zone_number")
    if _ITU_ZONES is None:
        _ITU_ZONES = _parse_geojson(_DATA_DIR / "ituzones.geojson", "itu_zone_number")


# --- point-in-polygon -----------------------------------------------------
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

def _lookup(lat, lon, zones):
    """First zone whose polygon contains (lat, lon), or None.

    Dateline-crossing polygons in the vendored data are stored unwrapped
    (longitudes run past ±180; -200 means 160°E). Longitude is periodic
    mod 360, so testing the point at lon±360 as well covers them exactly.
    The bbox prefilter rejects the shifted copies instantly for the ~all
    zones that don't wrap, so the extra cost is two tuple comparisons.
    """
    for zone in zones:
        for lo in (lon, lon - 360.0, lon + 360.0):
            if not _point_in_bbox(lat, lo, zone["bbox"]):
                continue
            if _point_in_polygon(lat, lo, zone["rings"]):
                return zone["number"]
    return None


# --- public API -----------------------------------------------------------
def derive(lat, lon):
    """{ 'cq_zone': int|None, 'itu_zone': int|None } for a coordinate.

    Never raises. Both fields are None when:
      - either input is None, non-numeric, NaN, or out of geographic range
      - or no zone polygon in either dataset covers the point.

    Lat must be in [-90, 90], lon in [-180, 180] — anything outside that
    is treated as bad input and returns (None, None) rather than risking
    a misleading hit from a bounding-box-near-edge polygon.
    """
    _ensure_loaded()

    try:
        if lat is None or lon is None:
            return {"cq_zone": None, "itu_zone": None}
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return {"cq_zone": None, "itu_zone": None}
    if math.isnan(lat_f) or math.isnan(lon_f):
        return {"cq_zone": None, "itu_zone": None}
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return {"cq_zone": None, "itu_zone": None}

    return {
        "cq_zone": _lookup(lat_f, lon_f, _CQ_ZONES),
        "itu_zone": _lookup(lat_f, lon_f, _ITU_ZONES),
    }