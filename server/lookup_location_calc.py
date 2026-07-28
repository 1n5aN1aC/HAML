"""Location-derived lookups: everything computed from a latitude/longitude.

Home for the derivations that turn a coordinate into some other geographic
fact about it — CQ and ITU zone numbers, DXCC entity and country, county,
state and ARRL/RAC section, Maidenhead gridsquare. The module is split so a
new derivation only has to add its own query + entry point:

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

A `process_*(record, ...)` establishes a position from something that is not
a coordinate — a park reference, a section, a state, a gridsquare. On
success it writes its own field and the record's coordinates in place and
returns the record. On failure it returns None having written nothing at
all, so the call is safe to use as a condition and a caller can fall through
to a coarser source. Follow one with `recalculate()` to derive the rest from
the position it set.

`recalculate_section_from_state()` sits outside the registry too, keyed on
the record's own state rather than on a coordinate.

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
import re
import sqlite3
import struct

import config
import lookup_record


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


# --- section from state -----------------------------------------------------

# state code -> section, for the states one section covers entirely. Read
# from `counties`, which carries `arrl_section` on every row, so the set of
# unambiguous states and the section each maps to come from the same table
# the polygon lookups use rather than a second copy kept here. 60 of 69
# states and provinces qualify; the nine that don't are CA (9 sections), NY
# and ON (4), FL and TX (3), and MA, NJ, PA and WA (2).
def _state_sections():
    """The state -> section map, or {} when the dataset can't answer."""
    conn = _conn()
    if conn is None:
        return {}
    try:
        return dict(conn.execute(
            "SELECT state, MIN(arrl_section) FROM counties "
            "GROUP BY state HAVING COUNT(DISTINCT arrl_section) = 1"))
    except sqlite3.Error as exc:
        print(f"warning: lookup dataset error reading sections: {exc}")
        return {}

def recalculate_section_from_state(record):
    """Set the record's section from its own state, in place, and return it.

    The one derivation here keyed on a record field rather than a
    coordinate, so it sits outside `_DERIVED_FIELDS` and `recalculate()`.
    A caller reaches for it when a state is all the record has.

    It writes only when the state names exactly one section, which is what
    separates it from `recalculate()`: a state that decides nothing leaves
    the existing section as it stands rather than nulling it. Both ways of
    not knowing behave the same — an unrecognised state, and a genuinely
    split one where the state alone cannot decide, since a California
    licensee could be in any of nine. To clear a section instead of keeping
    it, `lookup_record.blank()` says that explicitly.

    A single section is not the state's own code: `DC` is `MDC`, `HI`, `GU`,
    `AS` and `MP` are all `PAC`, and `NU` is `TER`.

    Never raises. A code in any case matches the table directly, which is
    what carries the US territories (`PR`, `VI`, `GU`, `AS`, `MP`) that
    `lookup_record._coerce_state` does not accept; a spelled-out US name
    falls back to that coercer to become a code first.
    """
    sections = _state_sections()
    state = record.get("state")
    code = state.strip().upper() if isinstance(state, str) else ""
    if code not in sections:
        code = lookup_record._coerce_state(state) or ""
    section = sections.get(code)
    if section is not None:
        record["section"] = section
    return record


# --- POTA park --------------------------------------------------------------

# What separates one reference from the next in an operator-typed park field
# on a park-to-park or multi-park activation. Comma alone: the field is not
# `freetext`, so the client's `sanitizeText` keeps only [A-Za-z0-9,_./-] and
# Space is the entry row's next-field key, and the client splits the same
# field on comma and nothing else.
_PARK_SPLIT = re.compile(r"[,]+")

def process_park(record, entry):
    """Move the record onto the park in `entry`, or answer None.

    Reads the operator-typed `their_park`, first reference only: a multi-park
    activation is one operating position. Sets `pota_park` to the park's name
    alongside the coordinates, so the key appears only on a record that is
    actually on a park.

    None covers no park typed, an unparseable field, a reference the table
    doesn't carry, and a park POTA lists without a real position — 2,736 hold
    (0, 0), its "coordinates unknown" placeholder, and 57 hold nothing.

    Matching is exact against `pota_parks.reference`, uppercased, so the
    table decides what counts as a park rather than a pattern here that would
    need widening every time POTA adds a numbering scheme.

    `pota_park` is not one of `lookup_record.FIELDS`: it is a request-time
    extra like `distance`, describing this contact rather than the callsign,
    so it belongs on the wire and not in the cache.
    """
    text = entry.get("their_park") if isinstance(entry, dict) else None
    if not isinstance(text, str):
        return None
    first = next((t for t in _PARK_SPLIT.split(text.strip()) if t), "")
    reference = first.strip(".,;:()[]").upper()
    if not reference:
        return None

    conn = _conn()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT latitude, longitude, name FROM pota_parks "
            "WHERE reference = ?", (reference,)).fetchone()
    except sqlite3.Error as exc:
        print(f"warning: lookup dataset error reading pota_parks: {exc}")
        return None
    if row is None:
        return None

    lat, lon, name = row
    # (0, 0) is POTA's "coordinates unknown" placeholder on 2,736 parks, not
    # a position in the Gulf of Guinea; 57 more carry no coordinates at all.
    if lat == 0 and lon == 0:
        return None
    coord = _valid_coord(lat, lon)
    if coord is None:
        return None

    record["latitude"], record["longitude"] = coord
    record["pota_park"] = name or None
    return record


# --- operator-population anchors (section, state) ---------------------------

# `coordinates` is TEXT "lat,lon", so both halves are cut out in SQL rather
# than pulled into Python: these queries scan a lot of licensee rows and the
# point is to never materialise them.
_OP_LAT = "CAST(substr(coordinates, 1, instr(coordinates, ',') - 1) AS REAL)"
_OP_LON = "CAST(substr(coordinates, instr(coordinates, ',') + 1) AS REAL)"

# Sample every Nth row rather than reading all 102,567 California licensees
# to place an anchor that's already accurate to tens of km at best.
# Sampling by `rowid` keeps coverage even — `LIMIT` would grab the first N
# rows, which in the (column, coordinates) index order are latitude-ordered
# and cluster at California's southern border.
#
# Across all 154 states and sections, sampling changes nothing for state,
# section or CQ zone; county moves for 22 and gridsquare for 12 — which
# callers blank for anchors anyway.
_OP_SAMPLE = 8

# Below this threshold, skip sampling and read all rows. Small territories
# like UM (3), AS (19), and NU (40) are already cheap to scan in full.
_OP_SAMPLE_MIN = 500

# Both licensee tables, filtered on one column, as a single result set. `?1`
# is the value; the FCC table answers for US sections and states, the ISED
# one for RAC sections and provinces, and a caller never has to know which.
# `divisor` is the sampling step, and has to appear in both arms: a single
# clause over the union would take the FCC rows and never reach the ISED ones.
def _operators_where(column, divisor):
    sample = f"AND rowid % {divisor} = 0 " if divisor > 1 else ""
    return (
        f"SELECT {_OP_LAT} AS lat, {_OP_LON} AS lon FROM fcc_operators "
        f"WHERE {column} = ?1 AND coordinates IS NOT NULL {sample}"
        f"UNION ALL "
        f"SELECT {_OP_LAT}, {_OP_LON} FROM ca_operators "
        f"WHERE {column} = ?1 AND coordinates IS NOT NULL {sample}")

# Each licensee as a unit vector on the sphere, which is what makes the mean
# below well defined. Averaging degrees instead breaks wherever a population
# crosses the antimeridian, and two of these genuinely do: the PAC section
# holds 3,937 licensees near -157 (Hawaii, Samoa) and 569 near +145 (Guam,
# the Marianas), whose arithmetic mean longitude is a point in California.
_OP_X = "cos(radians(lat)) * cos(radians(lon))"
_OP_Y = "cos(radians(lat)) * sin(radians(lon))"
_OP_Z = "sin(radians(lat))"

def _operator_anchor(column, value):
    """Where the licensees of one section or state sit, as (lat, lon).

    Their mean position, snapped to the nearest licensee to it. The mean
    alone is a point in empty space — it lands in a lake, over a border, or
    off the coast for a section shaped around a bay — and every field this
    module derives from a coordinate would then answer for wherever that
    happens to be. Snapping to the closest real licensee keeps the answer
    inside the population it came from, which is the thing being estimated:
    where an operator from here probably is.

    The mean is taken over unit vectors and the snap minimises straight-line
    distance through the sphere, which ranks the same as distance across it.
    Both are done that way so the antimeridian and the poles need no special
    case, rather than for the geometry: degrees of longitude are not
    comparable to degrees of latitude, and neither wraps.

    None when the value names nobody, or when the dataset is unavailable —
    including a sqlite built without its math functions, which the error
    path below reports rather than answering wrongly.

    Both statements read the sampled licensee rows of the value (see
    `_OP_SAMPLE`), so the cost is what the indexes on `arrl_section` and
    `state` allow, over an eighth of the rows.
    """
    conn = _conn()
    if conn is None:
        return None
    try:
        # Sampled first, whole population second. The second pass is what
        # carries the values too small to sample, and it is free where it
        # fires: a value under `_OP_SAMPLE_MIN` sampled rows is one whose
        # full scan costs a millisecond.
        for divisor in (_OP_SAMPLE, 1):
            rows = _operators_where(column, divisor)
            x, y, z, count = conn.execute(
                f"SELECT AVG({_OP_X}), AVG({_OP_Y}), AVG({_OP_Z}), COUNT(*) "
                f"FROM ({rows})", (value,)).fetchone()
            if count and count >= _OP_SAMPLE_MIN:
                break
        if not count or x is None or y is None or z is None:
            return None
        row = conn.execute(
            f"SELECT lat, lon FROM ({rows}) "
            f"ORDER BY ({_OP_X} - ?2) * ({_OP_X} - ?2) "
            f"       + ({_OP_Y} - ?3) * ({_OP_Y} - ?3) "
            f"       + ({_OP_Z} - ?4) * ({_OP_Z} - ?4) LIMIT 1",
            (value, x, y, z)).fetchone()
    except sqlite3.Error as exc:
        print(f"warning: lookup dataset error anchoring {column}: {exc}")
        return None

    return _valid_coord(row[0], row[1]) if row else None

def process_section(record, section):
    """Move the record onto `section`'s licensee population, or answer None.

    The coordinate is where that section's licensees are, per
    `_operator_anchor()` — an estimate of a person from a region, not a
    position, and everything derived from it inherits that: county and
    gridsquare in particular are the anchor licensee's, not this operator's.

    None means the section names no licensee with coordinates.
    """
    code = lookup_record._coerce_upper(section)
    if code is None:
        return None
    coord = _operator_anchor("arrl_section", code)
    if coord is None:
        return None
    record["latitude"], record["longitude"] = coord
    record["section"] = code
    return record

def process_state(record, state):
    """Move the record onto `state`'s licensee population, or answer None.

    `process_section()` with the state column, and the same caveat with more
    force behind it: the anchor for Texas is one point standing in for
    1,300 km of it.

    Matched as typed first, which is what carries the US territories
    `lookup_record._coerce_state` rejects, then through that coercer so a
    spelled-out name becomes a code. The code written to the record is
    whichever one found the anchor.

    None means the state names no licensee with coordinates.
    """
    code = state.strip().upper() if isinstance(state, str) else ""
    coord = _operator_anchor("state", code) if code else None
    if coord is None:
        code = lookup_record._coerce_state(state)
        coord = _operator_anchor("state", code) if code else None
    if coord is None:
        return None
    record["latitude"], record["longitude"] = coord
    record["state"] = code
    return record


# --- Maidenhead gridsquare --------------------------------------------------

# Field letters for the first pair (20° of longitude, 10° of latitude each)
# and, at 4 characters, the digits of the square are 0-9 by construction.
_GRID_FIELD = "ABCDEFGHIJKLMNOPQR"

def derive_gridsquare(lat, lon):
    """{ 'gridsquare': str|None } — the 4-character Maidenhead locator.

    Arithmetic on the coordinate, not a polygon, so this is the one
    derivation that touches no table and answers for every point on Earth:
    None means only that the coordinate isn't usable (see `_valid_coord`).

    Maidenhead counts from the antipode of the prime meridian at the south
    pole, so both axes shift positive first. The pair of poles and the
    antimeridian are the boundaries the arithmetic has to survive: latitude
    +90 and longitude +180 land exactly on the far edge of the last field,
    which is why each axis is held just inside it. The `_valid_coord` remap
    of +180 to -180 puts the antimeridian in `A`, the field that starts
    there.
    """
    coord = _valid_coord(lat, lon)
    if coord is None:
        return {"gridsquare": None}
    lat_f, lon_f = coord

    lon_adj = min(lon_f + 180.0, 359.999999)
    lat_adj = min(lat_f + 90.0, 179.999999)
    return {"gridsquare": (
        _GRID_FIELD[int(lon_adj // 20)]
        + _GRID_FIELD[int(lat_adj // 10)]
        + str(int(lon_adj % 20 // 2))
        + str(int(lat_adj % 10))
    )}

def process_gridsquare(record, gridsquare):
    """Move the record to the centre of `gridsquare`, or answer None.

    The inverse of `derive_gridsquare()`, and lossy the same way: a
    4-character square is 2 degrees of longitude by 1 of latitude, so the
    centre is up to ~111 km from the operator. It is the best single point
    the grid names, and `derive_gridsquare()` on it returns the same square.

    Input and output both pass through `lookup_record._coerce_gridsquare`,
    so a longer or lowercase locator lands as the uppercase 4-character form
    the record's field holds, which is what the coordinates encode.

    None means the locator isn't a usable 4-character square.
    """
    grid = lookup_record._coerce_gridsquare(gridsquare)
    if grid is None:
        return None
    # Undo the arithmetic in derive_gridsquare(), then step to the middle of
    # the square: half of its 2 x 1 degrees.
    lon = (_GRID_FIELD.index(grid[0]) * 20.0 + int(grid[2]) * 2.0) - 180.0 + 1.0
    lat = (_GRID_FIELD.index(grid[1]) * 10.0 + int(grid[3]) * 1.0) - 90.0 + 0.5
    coord = _valid_coord(lat, lon)
    if coord is None:
        return None
    record["latitude"], record["longitude"] = coord
    record["gridsquare"] = grid
    return record


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
    "gridsquare": (derive_gridsquare, "gridsquare"),
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