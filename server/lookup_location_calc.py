"""Location-derived lookups: everything computed from a latitude/longitude.

Home for the derivations that turn a coordinate into some other geographic
fact about it — CQ and ITU zone numbers, DXCC entity and country, county,
state and ARRL/RAC section. The module is split so a new derivation only has
to add its own query + entry point:

  - shared coordinate handling (`_valid_coord`),
  - shared sqlite access, WKB parsing and point-in-polygon machinery,
  - one section per derivation, holding its query and its `derive_*`, and
  - `_DERIVED_FIELDS` + `recalculate()`, mapping record fields to whichever
    derivation answers them.

A derivation exposes two levels: a `derive_*(lat, lon)` that answers for a
bare coordinate, and `recalculate(record, fields)`, which writes any set of
answers straight into a canonical lookup record (`lookup_record.FIELDS`).
The record-level half lives here rather than in the caller so the field
name, the coordinate source, and the derivation that fills it stay in one
place.

Several fields come from one table: `county`, `state` and `section` are one
row of `counties`, and `country` and `dxcc` one row of `dxcc_entities`.
`recalculate()` groups the fields it is asked for by derivation, so each
query runs at most once per call however many of its fields are wanted.

Polygons come from the region tables of `lookup_data.sqlite` — the same file
`lookup_db.py` opens for the operator datasets. This module holds its own
read-only handle on it, because its public functions take a bare coordinate
and never see the app dict; a second read-only handle on the same file costs
nothing. The handle opens lazily on first use, so modules that merely
`import lookup_location_calc` (e.g. an offline provider) pay nothing until a
lookup fires, and geometry parses per lookup and is held no longer, so the
module's memory is its code.

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

# Geometry is parsed per lookup rather than cached. Parsing typical parts is
# negligible; caching unusually large polygons would use substantial memory
# (Baffin Island grows from 15.67 MB to about 110 MB when parsed) to avoid a
# rare ~145 ms parse.
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

def _lookup_region_row(table, columns, lat, lon, order="z.id"):
    """Columns of the first region in `table` whose polygon covers the point.

    `columns` is the SELECT list, qualified `z.`, and the return is that
    tuple — one derivation wants a zone number, another a name and a code, a
    third three administrative names, and each comes out of one feature row.

    The R*Tree (`{table}_bbox`) is a prefilter, not an answer: it matches
    parts whose *rectangle* contains the point, and the ray-cast above is
    what decides. So the per-point cost scales with the few parts that
    overlap the point rather than the full part count.

    Three things the query depends on:
      - the bbox comparison reads `minx <= lon AND maxx >= lon`, which is the
        direction that selects boxes containing the point;
      - geometry joins to the R*Tree on `p.part_id = b.id`, so each row
        carries exactly the one part whose box matched;
      - `order` decides overlaps deterministically, and each table needs its
        own rule. `z.id` suits the zones (ids ascend with the zone number)
        and `dxcc_entities` (ids ascend with polygon area, smallest first,
        which resolves an enclave like Vatican-inside-Italy to the enclave);
        `counties` takes a US-first tie-break so a point on the 49th
        parallel lands on one side of the border every time. None of these
        datasets is a partition — they overlap in places and leave gaps —
        so a point matches two regions or none. None is a correct answer and
        comes back as None.
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            f"SELECT p.geom, {columns} "
            f"FROM {table}_bbox b "
            f"JOIN {table}_parts p ON p.part_id = b.id "
            f"JOIN {table}       z ON z.id      = b.feature_id "
            f"WHERE b.minx <= ? AND b.maxx >= ? AND b.miny <= ? AND b.maxy >= ? "
            f"ORDER BY {order}",
            (lon, lon, lat, lat)).fetchall()
    except sqlite3.Error as exc:
        # The file is gone or unreadable: warn and answer None.
        print(f"warning: lookup dataset error deriving {table}: {exc}")
        return None
    for row in rows:
        if _point_in_polygon(lat, lon, _wkb_rings(row[0])):
            return tuple(row[1:])
    return None

def _lookup_region_value(table, column, lat, lon, order="z.id"):
    """`_lookup_region_row()` for the single-column case."""
    row = _lookup_region_row(table, f"z.{column}", lat, lon, order)
    return row[0] if row else None


# --- CQ / ITU zones ---------------------------------------------------------

# The two zone systems are separate tables and separate answers, so each has
# its own entry point and one query. Asking `recalculate()` for both fields
# runs both; asking for one runs one.
def derive_cq_zone(lat, lon):
    """{ 'cq_zone': int|None } for a coordinate.

    None when the coordinate isn't usable (see `_valid_coord`), when the
    dataset is unavailable, or when no CQ polygon covers the point.
    """
    coord = _valid_coord(lat, lon)
    if coord is None:
        return {"cq_zone": None}
    return {"cq_zone": _lookup_region_value("cq_zones", "zone", *coord)}

def derive_itu_zone(lat, lon):
    """{ 'itu_zone': int|None } for a coordinate.

    Same three sources of None as `derive_cq_zone()`, decided against the
    ITU polygons, so one zone system can resolve while the other doesn't.
    """
    coord = _valid_coord(lat, lon)
    if coord is None:
        return {"itu_zone": None}
    return {"itu_zone": _lookup_region_value("itu_zones", "zone", *coord)}


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


# --- county / state / section -----------------------------------------------

def derive_county(lat, lon):
    """{ 'county': str|None, 'state': str|None, 'section': str|None }.

    All three come from one row of `counties`, the file's only
    administrative geography, so they resolve together or not at all.

    Never raises, with the same three sources of None as the other
    derivations: an unusable coordinate, an unavailable dataset, or a point
    outside every polygon — here meaning outside the US and Canada, which
    is most of the world.

    The vocabulary is whatever the authoritative source calls its
    county-equivalent, which is not always a county: Connecticut has nine
    planning regions (W1AW sits in `Capitol`, and there is no `Hartford`),
    Louisiana has parishes, Alaska has boroughs and census areas, and
    Canadian rows are census divisions named like `Division No. 18`.

    `state` carries US state codes and Canadian province codes alike, and
    `section` the matching ARRL or RAC section — populated on every row, and
    the same values `fcc_operators.arrl_section` and
    `ca_operators.arrl_section` carry, since one importer derives all three.
    """
    coord = _valid_coord(lat, lon)
    if coord is None:
        return {"county": None, "state": None, "section": None}
    lat_f, lon_f = coord

    # US-first tie-break: a border point resolves to the US side every time.
    row = _lookup_region_row(
        "counties", "z.county, z.state, z.arrl_section", lat_f, lon_f,
        order="(z.country = 'US') DESC, z.id")
    if row is None:
        return {"county": None, "state": None, "section": None}
    county, state, section = row
    return {
        "county": county or None,
        "state": state or None,
        "section": section or None,
    }


# --- writing derivations onto a record --------------------------------------

# Which derivation answers each record field, and under which key. One entry
# per field the module can fill; `recalculate()` groups by derivation so a
# call runs each underlying query at most once no matter how many of its
# fields are asked for.
#
# `country` comes from `dxcc_entities` rather than `counties`, whose own
# `country` column is a two-letter `US`/`CA` code rather than an entity name.
_DERIVED_FIELDS = {
    "cq_zone":  (derive_cq_zone,  "cq_zone"),
    "itu_zone": (derive_itu_zone, "itu_zone"),
    "country":  (derive_dxcc,     "country"),
    "dxcc":     (derive_dxcc,     "dxcc"),
    "county":   (derive_county,   "county"),
    "state":    (derive_county,   "state"),
    "section":  (derive_county,   "section"),
}

def recalculate(record, fields=None):
    """Rewrite location-derived fields of `record` from its coordinates.

    `fields` names which to rewrite; None means all of `_DERIVED_FIELDS`.
    Each named field is overwritten with what the coordinate says, including
    with None when nothing covers the point — the coordinate is the
    authority for exactly the fields the caller asks about.

    A record without coordinates comes back untouched: there is nothing to
    derive from, so source-supplied values stay as they are.

    Fields sharing a derivation share its query, so asking for `county`,
    `state` and `section` together reads `counties` once.
    """
    if record.get("latitude") is None or record.get("longitude") is None:
        return record

    wanted = list(_DERIVED_FIELDS) if fields is None else list(fields)
    derived = {}
    for field in wanted:
        entry = _DERIVED_FIELDS.get(field)
        if entry is None:
            print(f"warning: {field} is not a location-derived field")
            continue
        derive, key = entry
        if derive not in derived:
            derived[derive] = derive(record["latitude"], record["longitude"])
        record[field] = derived[derive][key]
    return record