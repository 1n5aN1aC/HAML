"""Location-derived lookups: everything computed from a latitude/longitude.

Home for the derivations that turn a coordinate into some other geographic
fact about it — CQ and ITU zone numbers, DXCC entity and country. The module
is split so a new derivation only has to add its own query + entry point:

  - shared coordinate handling (`_valid_coord`),
  - shared sqlite access, WKB parsing and point-in-polygon machinery, and
  - one section per derivation, holding its query and its public functions.

A derivation exposes two levels: a `derive_*(lat, lon)` that answers for a
bare coordinate, and `recalculate_*(record)` wrappers that write the answer
straight into a canonical lookup record (`lookup_record.FIELDS`). The
wrappers live here rather than in the caller so the field name, the
coordinate source, and the derivation that fills it stay in one place.

Polygons come from the region tables of `lookup_data.sqlite` — the same file
`lookup_db.py` opens for the operator datasets. This module holds its own
read-only handle on it, because its public functions take a bare coordinate
and never see the app dict; a second read-only handle on the same file costs
nothing. The handle opens lazily on first use, so modules that merely
`import lookup_location_calc` (e.g. an offline provider) pay nothing until a
lookup fires, and each polygon parses at most once and is then kept.

Every public function here must never raise. Bad inputs (None, NaN,
out-of-range, non-numeric), a missing or corrupt dataset, and points no
polygon covers (open ocean, say) all return None for the affected field, so
callers can use the result without validating it.

Coordinate convention:
  - Callers pass (lat, lon). Standard geographic order.
  - The R*Tree and the WKB blobs are (x, y) — that is (lon, lat).
  - We swap on read so the rest of this module reasons in (lat, lon).
  - Zone geometry in the DB is clipped to ±180, so no polygon runs past the
    antimeridian and no unwrapping is needed.
"""
import math
import sqlite3
import struct

import config


# --- shared coordinate handling --------------------------------------------

def _valid_coord(lat, lon):
    """(lat, lon) as floats, or None if the pair isn't a usable coordinate.

    One gate in front of every derivation, so they all reject the same
    things the same way: None, non-numeric, NaN, or outside the geographic
    range (lat [-90, 90], lon [-180, 180]). Out-of-range is rejected rather
    than clamped or wrapped — a coordinate that far off is bad input, and
    testing it anyway risks a misleading hit from a polygon whose bounding
    box happens to reach the edge.

    Longitude +180 comes back as -180. Both name the antimeridian, the zone
    polygons are clipped there, and the ray-cast below casts rightward, so
    -180 is the side of that meridian the polygons cover.
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
    if lon_f == 180.0:
        lon_f = -180.0
    return lat_f, lon_f


# --- shared sqlite handle ---------------------------------------------------

# Lazily opened by _conn(); _TRIED separates "not opened yet" from "opened and
# failed", so a missing dataset is diagnosed (and warned about) exactly once.
_CONN = None
_TRIED = False

def _conn():
    """The read-only handle on the lookup dataset, or None if unusable.

    Read-only for the same reason `lookup_db` is: the importer replaces these
    tables in one transaction, and a stray writer can make a publish fail.
    A missing or corrupt file is not fatal — every derivation simply answers
    None, exactly as it does for a point no polygon covers.

    The path is `lookup_db_path` from the default config location, since
    there is no app dict here to carry the running server's config.
    """
    global _CONN, _TRIED
    if _CONN is None and not _TRIED:
        _TRIED = True
        try:
            db_path = config.load_config()["lookup_db_path"]
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            # Touch a region table so an unusable file is caught here, once,
            # with the connection still in hand.
            conn.execute("SELECT 1 FROM cq_zones LIMIT 1").fetchone()
            _CONN = conn
        except (sqlite3.Error, OSError, KeyError, ValueError) as exc:
            print(
                f"warning: lookup dataset unavailable for location "
                f"derivations ({exc}); CQ/ITU zones will not be derived"
            )
    return _CONN


# --- shared WKB parsing -----------------------------------------------------

# Parsed rings keyed by (table, part_id). The zone tables hold 147 parts
# totalling well under a megabyte, so every part a lookup touches is kept:
# the file changes only by being rebuilt, and the process reads it once.
_RINGS = {}

def _wkb_rings(blob):
    """Rings of a WKB polygon as lists of (lat, lon) tuples, outer ring first.

    The region tables store one polygon part per row, always WKB type 3
    (Polygon) with no SRID prefix. Both byte orders are honoured because the
    format allows either. Anything else — another geometry type, a truncated
    blob — yields no rings, so one bad part is skipped rather than poisoning
    the lookup.
    """
    try:
        prefix = "<" if blob[0] == 1 else ">"
        gtype, nrings = struct.unpack_from(prefix + "II", blob, 1)
        if gtype != 3:
            return []
        offset = 9
        rings = []
        for _ in range(nrings):
            npoints, = struct.unpack_from(prefix + "I", blob, offset)
            offset += 4
            # One unpack for the whole ring: the pairs are (lon, lat) and
            # come back flat, so step by two and swap into (lat, lon).
            flat = struct.unpack_from(f"{prefix}{npoints * 2}d", blob, offset)
            offset += npoints * 16
            rings.append([(flat[i + 1], flat[i])
                          for i in range(0, npoints * 2, 2)])
        return rings
    except (struct.error, IndexError, TypeError):
        return []

def _rings_of_part(table, part_id, blob):
    """Cached `_wkb_rings()` for one part row."""
    key = (table, part_id)
    rings = _RINGS.get(key)
    if rings is None:
        rings = _wkb_rings(blob)
        _RINGS[key] = rings
    return rings


# --- shared point-in-polygon ------------------------------------------------

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

def _lookup_region_row(table, columns, lat, lon):
    """Columns of the first region in `table` whose polygon covers the point.

    `columns` is the SELECT list, qualified `z.`, and the return is that
    tuple — one derivation wants a zone number, another a name and a code,
    and both come out of the same feature row.

    The R*Tree (`{table}_bbox`) is a prefilter, not an answer: it matches
    parts whose *rectangle* contains the point, and the ray-cast above is
    what decides. So the per-point cost scales with the few parts that
    overlap the point rather than the full part count.

    Three things the query depends on:
      - the bbox comparison reads `minx <= lon AND maxx >= lon`, which is the
        direction that selects boxes containing the point;
      - geometry joins to the R*Tree on `p.part_id = b.id`, so each row
        carries exactly the one part whose box matched;
      - `ORDER BY z.id` decides overlaps deterministically, and each table
        orders its ids for that purpose: CQ and ITU ascend with the zone
        number, `dxcc_entities` ascends with polygon area, smallest first,
        which is what resolves an enclave (Vatican inside Italy, say) to the
        enclave. None of these datasets is a partition — they overlap in
        places and leave gaps — so a point matches two regions or none. None
        is a correct answer and comes back as None.
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            f"SELECT p.part_id, p.geom, {columns} "
            f"FROM {table}_bbox b "
            f"JOIN {table}_parts p ON p.part_id = b.id "
            f"JOIN {table}       z ON z.id      = b.feature_id "
            f"WHERE b.minx <= ? AND b.maxx >= ? AND b.miny <= ? AND b.maxy >= ? "
            f"ORDER BY z.id",
            (lon, lon, lat, lat)).fetchall()
    except sqlite3.Error as exc:
        # The file is gone or unreadable: warn and answer None.
        print(f"warning: lookup dataset error deriving {table}: {exc}")
        return None
    for row in rows:
        part_id, geom = row[0], row[1]
        if _point_in_polygon(lat, lon, _rings_of_part(table, part_id, geom)):
            return tuple(row[2:])
    return None

def _lookup_region_value(table, column, lat, lon):
    """`_lookup_region_row()` for the single-column case."""
    row = _lookup_region_row(table, f"z.{column}", lat, lon)
    return row[0] if row else None


# --- CQ / ITU zones ---------------------------------------------------------

def derive_zones(lat, lon):
    """{ 'cq_zone': int|None, 'itu_zone': int|None } for a coordinate.

    Never raises. A field is None when the coordinate isn't usable (see
    `_valid_coord`), when the dataset is unavailable, or when no polygon in
    that dataset covers the point. The two zone systems are looked up
    independently, so one can resolve while the other doesn't.
    """
    coord = _valid_coord(lat, lon)
    if coord is None:
        return {"cq_zone": None, "itu_zone": None}
    lat_f, lon_f = coord

    return {
        "cq_zone": _lookup_region_value("cq_zones", "zone", lat_f, lon_f),
        "itu_zone": _lookup_region_value("itu_zones", "zone", lat_f, lon_f),
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


# --- DXCC entity ------------------------------------------------------------

def derive_dxcc(lat, lon):
    """{ 'country': str|None, 'dxcc': int|None } for a coordinate.

    Both fields come from one row of `dxcc_entities`: `name` is the entity
    name the canonical record calls `country`, `entity_code` is the ARRL
    number it calls `dxcc`. They resolve together or not at all.

    Never raises. Both are None when the coordinate isn't usable (see
    `_valid_coord`), when the dataset is unavailable, or when no entity
    polygon covers the point.

    Three properties of this dataset shape what a caller can trust:
      - it is land only, ~33.8% of the globe, so any maritime coordinate is
        a legitimate None;
      - the coastlines are a 1:110m generalisation (~16,600 vertices
        worldwide) with neighbouring borders generalised independently, so a
        point within a few km of a land border resolves on the geometry's
        idea of that border rather than the real one;
      - the upstream polygons omit part of Vanuatu (Efate, Tanna, Erromango,
        the Banks/Torres groups) and cover Palestine as the West Bank only.

    The entity vocabulary is DXCC's own: `K` is "United States of America",
    matching what `lookup_callparser` returns and not the shorter
    "United States" the FCC table carries.
    """
    coord = _valid_coord(lat, lon)
    if coord is None:
        return {"country": None, "dxcc": None}
    lat_f, lon_f = coord

    row = _lookup_region_row(
        "dxcc_entities", "z.name, z.entity_code", lat_f, lon_f)
    if row is None:
        return {"country": None, "dxcc": None}
    name, entity_code = row
    return {"country": name or None, "dxcc": entity_code}

# Set the record's country from its coordinates, overwriting any value.
# Same no-coordinates rule as recalculate_cq_zone.
def recalculate_country(record):
    if record.get("latitude") is None or record.get("longitude") is None:
        return record
    record["country"] = derive_dxcc(
        record["latitude"], record["longitude"])["country"]
    return record

# Set the record's DXCC entity code from its coordinates, overwriting any
# value. Same no-coordinates rule as recalculate_cq_zone.
def recalculate_dxcc(record):
    if record.get("latitude") is None or record.get("longitude") is None:
        return record
    record["dxcc"] = derive_dxcc(
        record["latitude"], record["longitude"])["dxcc"]
    return record