#!/usr/bin/env python3
r"""
importer_boundaries.py - US county and Canadian census-division boundaries.

One of the importers driven by run_importers.py (which calls run() directly;
this file is also runnable on its own). It downloads two boundary sources,
probing each newest-first at run time rather than pinning a vintage, converts
them to WGS84, and publishes three objects of lookup_data.sqlite:

    counties              (id, county, state, country, arrl_section)
    counties_parts        (part_id INTEGER PRIMARY KEY, feature_id, geom)
    counties_bbox  rtree(id, minx, maxx, miny, maxy, +feature_id)

US counties come from the Census, Canadian census divisions from StatCan; the
table holds the US rows first. On a network failure the run falls back to the
newest complete set already in downloads/.

This table is the only geography published here: `state` comes back with every
hit, so it answers "what state is this point in" as well. Whole-state geometry
is not available from it - dissolving a state's internal county borders is a
unary_union over every part, the quadratic noding operation repair() exists to
avoid - so take state outlines from the source shapefile instead.

`state` is always exactly the two-letter state/territory/province code (Canadian
codes come from PRUID, not StatCan's "B.C."-style PREABBR) and `country` is 'US'
or 'CA'. `geom` is a single WKB Polygon in WGS84 (EPSG:4326), lon/lat, stored at
full source resolution (296.5 MB, nearly all Canadian coastline); see CA_SIMPLIFY
for why simplifying does not help.

`arrl_section` is the ARRL/RAC contest section the county belongs to, derived
here rather than sourced - neither the Census nor StatCan knows what a section
is. It is a property of the county, so a point-in-polygon hit answers "what
section is this?" in the same row that answers "what county is this?", with no
second lookup. Most states and provinces are one section named after their own
code; the mapping is by county name only in the 8 split US states and Ontario
(see SPLIT_SECTIONS in sections.py). NULL means the name is not in those
tables - a renamed or newly split county - which Phase 3 reports rather than
passes off as mapped.

Geometry and boxes are ONE POLYGON PART PER ROW, not per feature: Nunavut alone
is 62,547 parts in one feature, so a per-feature box means one hit drags in the
whole Arctic archipelago (524.8 us/pt versus 11.5 us/pt, identical answers).
`part_id` is the rowid and is exactly the matching `_bbox.id`, so the lookup
join is a rowid seek; `feature_id` is only for reassembly (order a feature's
parts by part_id - they are disjoint, so no union is needed) and verification.

Querying
--------
The R*Tree is a PREFILTER, not an answer - it tests bounding boxes - so the
exact test has to be done in shapely:

    SELECT p.geom, c.county, c.state, c.country, c.arrl_section
      FROM counties_bbox b
      JOIN counties_parts p ON p.part_id = b.id
      JOIN counties       c ON c.id      = b.feature_id
     WHERE b.minx <= ? AND b.maxx >= ? AND b.miny <= ? AND b.maxy >= ?
     ORDER BY (c.country = 'US') DESC, c.id

...then take the first row whose wkb.loads(geom).covers(pt). Four details, each
of which fails silently rather than raising:

  1. The bbox test reads backwards: `minx <= lon AND maxx >= lon`. The other
     way round still returns rows, just the wrong ones.
  2. ORDER BY country='US' DESC is the border tie-break; without it a point on
     the 49th parallel resolves arbitrarily.
  3. `covers`, not `contains`, which is False exactly on the boundary and so
     drops shared edges between adjacent counties.
  4. Join geometry on `p.part_id = b.id`, NOT feature_id - by feature_id it
     still answers correctly, at the per-feature cost this schema exists to
     avoid, so nothing ever tells you.

Do not add DISTINCT: every part is a different row with different geometry, so
it collapses nothing and merely sorts rows carrying blobs. The R*Tree rounds its
32-bit float bounds OUTWARD, so the prefilter yields false positives but never
false negatives; covers() removes them.

This table also answers "what state is this point in" - `state` comes back with
every hit. For bulk work, load every geometry once, shapely.prepare() them, and
use an in-memory STRtree instead.

Every stored row is a valid Polygon, checked on the whole geometry before it is
split. That is not cosmetic: GEOS covers/contains against a self-intersecting
ring can return the WRONG answer rather than an error, and rounding after
reprojecting Canada out of Lambert metres produces exactly that. There is no
flag to skip the repair.

Usage
-----
    .venv\Scripts\python run_importers.py         # the menu; Boundaries is 4
    .venv\Scripts\python importer_boundaries.py   # this importer alone

There are no options. Every path is fixed under the project root: the three
objects above in lookup_data.sqlite, the two source archives in downloads/,
caches/boundaries_work.sqlite, and logs/boundaries_run.log. Requires requests,
pyshp, pyproj and shapely.

The US county archive is shared with importer_fcc.py, which downloads the same
file for its own point-in-polygon phase; the StatCan census-division archive is
shared with importer_ca.py. Neither is this importer's alone to delete.
"""

import argparse
import io
import os
import re
import sqlite3
import sys
import time
import zipfile

import sections

# --- Constants ------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(HERE, "downloads")
CACHES_DIR = os.path.join(HERE, "caches")
LOGS_DIR = os.path.join(HERE, "logs")

DB_PATH = os.path.join(HERE, "lookup_data.sqlite")
WORK_DB = os.path.join(CACHES_DIR, "boundaries_work.sqlite")
RUN_LOG = "boundaries_run.log"

COUNTIES_TABLE = "counties"

# requests' default User-Agent is a common target for blanket bot rules on
# public .gov / .gc.ca endpoints, which surface as a 403.
HTTP_HEADERS = {"User-Agent": "boundary-import/1.0 (+lookup_data build)"}

# Decimal places (~11 cm) coordinates are rounded to after reprojection. Not
# cosmetic and not configurable - the degenerate-ring handling exists because
# of it.
PRECISION = 6

# Simplification tolerance in degrees, or None. LEAVE THIS None. These are
# already generalized cartographic files whose size is the number of ISLANDS,
# not vertex spacing (Nunavut is 62,547 separate polygons), so 0.001 (~100 m)
# buys ~1% of size. Worse, shapely's preserve_topology=True is superlinear in
# part count: on Nunavut it ran for minutes at 3.9 GB of RAM without finishing.
# The lever that would shrink this table is dropping islands below an area
# threshold, which has a real correctness cost: a licensee on a dropped island
# falls outside every polygon and gets no county.
CA_SIMPLIFY = None

# US Census cartographic boundary file (1:500,000 generalized): ~11 MB for all
# counties versus ~150 MB for the 1:500 TIGER equivalent, with detail beyond
# what a point-in-polygon lookup can use. It is NAD83 (EPSG:4269); the sub-metre
# difference from WGS84 is far below the ~100 m generalization error already
# baked in, so it is emitted with no reprojection. importer_fcc.py downloads
# this same file for its own county phase.
US_COUNTY_URL = ("https://www2.census.gov/geo/tiger/GENZ{y}/shp/"
                 "cb_{y}_us_county_500k.zip")
US_COUNTY_FILE = "cb_{y}_us_county_500k.zip"
US_VINTAGE_FLOOR = 2019    # probe floor: STUSPS is present from this vintage

# StatCan census boundary file. Native CRS is NAD83 / Statistics Canada Lambert
# and MUST be reprojected; the raw coordinates are metres in the millions. Years
# are newest first - 2021 is current, and 2026 is the next release, probed so it
# is picked up without an edit. importer_ca.py reads this same file for its
# province interior points.
CA_BASE = ("https://www12.statcan.gc.ca/census-recensement/{year}/geo/sip-pis/"
           "boundary-limites/files-fichiers/{name}")
CA_CD_FILE = "lcd_000b{yy}a_e.zip"      # census divisions ("counties")
CA_CENSUS_YEARS = [2026, 2021]
CA_CRS = "EPSG:3347"

# PREABBR in the file is "B.C." / "N.L.", not the two-letter code the schema
# contract asks for.
PRUID_TO_PROV = sections.PRUID_TO_PROV

# --- ARRL / RAC sections --------------------------------------------------- #
#
# The same tables importer_fcc.py (Phase 9) and importer_ca.py (Phase 11) use to
# section an operator, applied here to the county itself. All three read them
# from sections.py, because all three must give the same answer: an operator
# tagged from its county and the county polygon it sits in cannot disagree
# about which section that is.
#
# What is shared is the Census NAME / StatCan CDNAME the section is keyed on.
# Names are matched EXACTLY as this importer stores them - Census NAME
# ("St. Johns", "Miami-Dade"), CDNAME after clean_name() ("Greater Sudbury") -
# so upstream renaming surfaces as an unmapped name in the Phase 4 report
# instead of quietly moving a county into the wrong section.

US_SECTION_BY_COUNTY = sections.SECTION_BY_COUNTY      # for the Phase 4 report
CA_ON_SECTION_BY_CD = sections.ON_SECTION_BY_CD        # ditto


def section_for(county, state, country):
    """ARRL/RAC section for one county row, or None if it cannot be mapped.

    Only the split US states and Ontario consult the name; everywhere else the
    state or province code alone decides, so a county rename there is harmless.
    """
    if country == "CA":
        return sections.ca_section(state, county)
    return sections.us_section(state, county)


# --- Logging (console + utf-8 log file) ------------------------------------ #

_log_fh = None
BANNER_RULE = "-" * 70
_notices = []


def log(msg=""):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}" if msg else ""
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def log_banner(lines):
    """Log a rule-delimited block, and repeat it at the end of the run."""
    log("")
    for line in [BANNER_RULE, *lines, BANNER_RULE]:
        log(line)
    log("")
    _notices.append(lines)


def replay_notices():
    if not _notices:
        return
    log("")
    log(BANNER_RULE)
    log(f" {len(_notices)} notice(s) from this run:")
    for lines in _notices:
        log("")
        for line in lines:
            log(line)
    log(BANNER_RULE)


# --- Phase 1 - cleanup ----------------------------------------------------- #

def cleanup_old_data():
    """Delete what a previous run stranded - deliberately not lookup_data.sqlite
    or the downloaded archives, which are replaced only once their replacements
    are complete."""
    victims = [WORK_DB, WORK_DB + "-journal"]
    for name in os.listdir(DOWNLOADS_DIR) if os.path.isdir(DOWNLOADS_DIR) else []:
        if name.endswith(".part"):
            victims.append(os.path.join(DOWNLOADS_DIR, name))
    removed = 0
    for path in victims:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                log(f"  could not remove {os.path.basename(path)} ({e})")
                continue
            log(f"  removed {os.path.basename(path)}")
            removed += 1
    log(f"Cleanup: {removed} stale file(s) removed. The published table and "
        f"the downloaded archives stay in place until replaced atomically.")


# --- Phase 2 - resolve the newest release, download it --------------------- #

def _url_exists(url):
    """True if `url` serves a real zip archive.

    A 100-byte ranged GET rather than HEAD: StatCan answers HEAD on a valid file
    with a redirect loop, and both servers serve a MISSING file as HTML under
    HTTP 200, so only the zip magic number settles it. The read timeout is
    deliberately short - GENZ2026 accepts the connection and never replies, and
    a stalled probe is indistinguishable from a missing file here."""
    import requests

    try:
        with requests.get(url, timeout=(5, 5), stream=True, allow_redirects=True,
                          headers={**HTTP_HEADERS, "Range": "bytes=0-99"}) as r:
            if r.status_code not in (200, 206):
                return False
            if "html" in r.headers.get("Content-Type", "").lower():
                return False
            return r.raw.read(2) == b"PK"
    except requests.RequestException:
        return False


def _local(filename):
    """Path to `filename` in downloads/ if it is there and is a readable zip."""
    p = os.path.join(DOWNLOADS_DIR, filename)
    return p if os.path.exists(p) and zipfile.is_zipfile(p) else None


def _local_vintages(template):
    """Vintages of an archive already in downloads/, newest first."""
    pat = re.escape(template).replace(r"\{y\}", r"(\d{4})").replace(r"\{yy\}",
                                                                   r"(\d{2})")
    out = []
    for name in os.listdir(DOWNLOADS_DIR) if os.path.isdir(DOWNLOADS_DIR) else []:
        m = re.fullmatch(pat, name)
        if m and zipfile.is_zipfile(os.path.join(DOWNLOADS_DIR, name)):
            out.append(int(m.group(1)))
    return sorted(out, reverse=True)


def fetch(url, label):
    """Download `url` into downloads/ once and reuse it thereafter.

    Lands via a .part file and is proved to be a readable zip before it takes
    the real name, so a later run never "reuses" a truncated or HTML-error one.
    Returns None on failure, leaving the caller to fall back."""
    import requests

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    filename = url.rsplit("/", 1)[-1]
    path = os.path.join(DOWNLOADS_DIR, filename)

    if _local(filename):
        log(f"{label}: using existing {filename}")
        return path

    log(f"{label}: downloading {url}")
    tmp = path + ".part"
    try:
        with requests.get(url, timeout=(30, 900), stream=True,
                          headers=HTTP_HEADERS) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        if not zipfile.is_zipfile(tmp):
            # Context manager: on Windows an open handle here would make the
            # cleanup below fail with "file in use".
            with open(tmp, "rb") as f:
                head = f.read(200).decode("utf-8", "replace")
            log(f"  {url} did not return a zip ({os.path.getsize(tmp):,} bytes). "
                f"Both Census and StatCan serve missing files as HTML, so this "
                f"usually means the URL moved. starts with: {head[:120]!r}")
            return None
        os.replace(tmp, path)
    except requests.RequestException as e:
        log(f"  download failed ({e})")
        return None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    log(f"{label}: {os.path.getsize(path) / 1e6:,.1f} MB -> {filename}")
    return path


def resolve_us():
    """(vintage, county_zip) for the newest Census release."""
    for y in range(time.gmtime().tm_year + 1, US_VINTAGE_FLOOR - 1, -1):
        if not _url_exists(US_COUNTY_URL.format(y=y)):
            continue
        c = fetch(US_COUNTY_URL.format(y=y), "US counties")
        if c:
            return y, c
        break        # found upstream but could not fetch - fall back below

    for y in _local_vintages(US_COUNTY_FILE):
        c = _local(US_COUNTY_FILE.format(y=y))
        if c:
            log_banner([
                " NOTE: could not reach the Census - using a local vintage",
                "",
                f"   Building US counties from vintage {y} already in",
                "   downloads/. THIS TABLE IS ONLY AS CURRENT AS THAT FILE.",
                "   Rerun once the Census is reachable again.",
            ])
            return y, c

    sys.exit(f"ERROR: no Census cartographic vintage could be downloaded at or "
             f"after {US_VINTAGE_FLOOR}, and no local copy exists in "
             f"{DOWNLOADS_DIR}")


def resolve_ca():
    """(year, cd_zip) for the newest StatCan census release."""
    for year in CA_CENSUS_YEARS:
        yy = f"{year % 100:02d}"
        cd_url = CA_BASE.format(year=year, name=CA_CD_FILE.format(yy=yy))
        if not _url_exists(cd_url):
            continue
        c = fetch(cd_url, "CA census divisions")
        if c:
            return year, c
        break

    for year in CA_CENSUS_YEARS:
        yy = f"{year % 100:02d}"
        c = _local(CA_CD_FILE.format(yy=yy))
        if c:
            log_banner([
                " NOTE: could not reach StatCan - using a local release",
                "",
                f"   Building Canadian census divisions from the {year} file",
                "   already in downloads/. THIS TABLE IS ONLY AS CURRENT AS",
                "   THAT FILE. Rerun once StatCan is reachable.",
            ])
            return year, c

    sys.exit(f"ERROR: no StatCan boundary release could be downloaded for "
             f"{CA_CENSUS_YEARS}, and no local copy exists in "
             f"{DOWNLOADS_DIR}")


# --- Shapefile reading ----------------------------------------------------- #

def read_shapefile_zip(zpath, encoding="utf-8"):
    """Yield (geo_interface_dict, record_dict) for every shape in a zipped .shp.

    Member names come from the archive rather than the local filename, so an
    upstream renaming needs no change here. StatCan's dbf is latin-1."""
    import shapefile as pyshp

    with zipfile.ZipFile(zpath) as zf:
        shp_name = next(n for n in zf.namelist() if n.lower().endswith(".shp"))
        base = shp_name[:-4]
        rdr = pyshp.Reader(
            shp=io.BytesIO(zf.read(base + ".shp")),
            shx=io.BytesIO(zf.read(base + ".shx")),
            dbf=io.BytesIO(zf.read(base + ".dbf")),
            encoding=encoding,
        )
        fields = [f[0] for f in rdr.fields[1:]]
        for sr in rdr.iterShapeRecords():
            if sr.shape.shapeType == 0 or not sr.shape.points:
                continue                      # null shape - nothing to emit
            yield sr.shape.__geo_interface__, dict(zip(fields, sr.record))


def require_fields(zpath, needed, why):
    """Fail before the long work starts if the source lost a field we key on."""
    import shapefile as pyshp

    with zipfile.ZipFile(zpath) as zf:
        shp = next(n for n in zf.namelist() if n.lower().endswith(".shp"))
        base = shp[:-4]
        rdr = pyshp.Reader(shp=io.BytesIO(zf.read(base + ".shp")),
                           dbf=io.BytesIO(zf.read(base + ".dbf")),
                           shx=io.BytesIO(zf.read(base + ".shx")))
        have = {f[0] for f in rdr.fields[1:]}
    missing = [f for f in needed if f not in have]
    if missing:
        sys.exit(f"ERROR: {os.path.basename(zpath)} is missing {missing} - {why}. "
                 f"Fields present: {sorted(have)}")


# --- Geometry conversion --------------------------------------------------- #

def _convert_ring(points, transform):
    """One reprojected, rounded, closed ring - or None if it collapsed.

    Rounding to 6 dp makes vertices that reprojection left centimetres apart
    *identical*, producing consecutive duplicates (GEOS: "Ring
    Self-intersection") and short rings that degenerate to a line; both are
    dropped here rather than left for make_valid to guess at. Every step runs
    over whole coordinate arrays because there are ~35 million vertices."""
    import numpy as np

    if len(points) < 3:
        return None
    try:
        arr = np.asarray(points, dtype=float)
        xs, ys = arr[:, 0], arr[:, 1]
    except (ValueError, IndexError):
        # Ragged input (mixed 2-D/3-D vertices) - not seen in either source,
        # but a loud fallback beats an exception a whole run dies on.
        xs = np.array([p[0] for p in points], dtype=float)
        ys = np.array([p[1] for p in points], dtype=float)
    if transform:
        xs, ys = transform(xs, ys)

    ring = np.column_stack((np.round(xs, PRECISION), np.round(ys, PRECISION)))

    # CONSECUTIVE duplicates only - the same vertex reappearing later in the
    # ring is legitimate, and collapsing those would change the shape.
    keep = np.empty(len(ring), dtype=bool)
    keep[0] = True
    np.any(ring[1:] != ring[:-1], axis=1, out=keep[1:])
    ring = ring[keep]

    if len(ring) < 3:
        return None
    out = ring.tolist()
    if out[0] != out[-1]:
        out.append(list(out[0]))
    # 4 = three distinct corners plus the closing repeat; less has no area.
    return out if len(out) >= 4 else None


def _convert_polygon(rings, transform):
    """Rings of one polygon, or None if the exterior ring did not survive. A
    collapsed hole is a rounding-scale error and is simply dropped."""
    conv = [_convert_ring(r, transform) for r in rings]
    if not conv or conv[0] is None:
        return None
    return [r for r in conv if r is not None]


def convert_geometry(geom, transform=None, on_repair=None, simplify=None):
    """Reproject, round, optionally simplify and repair one geometry. Returns a
    shapely geometry, or None if nothing usable survives."""
    from shapely.geometry import shape as shapely_shape

    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return None

    if gtype == "Polygon":
        rings = _convert_polygon(coords, transform)
        if not rings:
            return None
        out = {"type": "Polygon", "coordinates": rings}
    elif gtype == "MultiPolygon":
        polys = [_convert_polygon(p, transform) for p in coords]
        polys = [p for p in polys if p]
        if not polys:
            return None
        out = {"type": "MultiPolygon", "coordinates": polys}
    else:
        # Both sources are polygonal; anything else is a source change worth
        # hearing about rather than silently storing.
        log(f"  WARNING: unexpected geometry type {gtype!r} - skipped")
        return None

    g = shapely_shape(out)

    if simplify is not None:
        # Before the validity check, not after: simplification is what can
        # pinch a ring, so it has to be the repair pass's input.
        g = g.simplify(simplify, preserve_topology=True)

    if not g.is_valid:
        g = repair(g, on_repair)

    return None if g is None or g.is_empty else g


def _polygons(g):
    """Every Polygon inside `g`, flattened. make_valid can hand back a
    GeometryCollection with stray lines where a ring pinched; only the
    polygonal parts are boundary."""
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [q for part in g.geoms for q in _polygons(part)]
    return []


def repair(g, on_repair=None):
    """Make an invalid Polygon/MultiPolygon valid, ONE PART AT A TIME.

    make_valid() on the whole MultiPolygon plus a unary_union() is effectively
    quadratic in part count, because the union nodes every polygon against every
    other - Nunavut 195.25 s versus 19.40 s per-part - for output valid and
    area-identical to 6 decimal places. Almost nothing is actually broken
    (Ontario: one bad polygon out of 37,244), so per-part repair pays make_valid
    only on those and disjoint islands never need noding at all. What it cannot
    fix is parts that OVERLAP each other, since that is a property of the
    collection; the result is re-checked and falls back to the slow
    whole-geometry union rather than assume no source is invalid that way."""
    import shapely
    from shapely.geometry import MultiPolygon
    from shapely.validation import make_valid

    was_polygon = g.geom_type == "Polygon"
    parts = [g] if was_polygon else list(g.geoms)

    # One vectorized GEOS call for every part, rather than one per part.
    flags = shapely.is_valid(parts)

    out, n_bad = [], 0
    for part, ok in zip(parts, flags):
        if ok:
            out.append(part)
            continue
        n_bad += 1
        out.extend(_polygons(make_valid(part)))

    if not out:
        return None
    if n_bad and on_repair:
        on_repair()

    fixed = out[0] if (was_polygon and len(out) == 1) else MultiPolygon(out)
    if fixed.is_valid:
        return fixed

    # Parts overlap one another, so the collection is invalid even though every
    # polygon in it is fine. Only the full union settles that one.
    from shapely.ops import unary_union
    log("    (parts overlap - falling back to whole-geometry union)")
    merged = unary_union(_polygons(make_valid(fixed)))
    if on_repair and not n_bad:
        on_repair()
    return merged


def ca_transformer():
    """EPSG:3347 -> EPSG:4326 (lon, lat), vectorized over coordinate arrays."""
    import pyproj
    return pyproj.Transformer.from_crs(CA_CRS, "EPSG:4326",
                                       always_xy=True).transform


def clean_name(name):
    """StatCan CDNAME cleanup: drop the bilingual duplication
    ('Greater Sudbury / Grand Sudbury' -> 'Greater Sudbury') and collapse the
    padding whitespace ('Division No.  6' -> 'Division No. 6')."""
    name = (name or "").split(" / ")[0]
    return " ".join(name.split()) or None


# --- Feature builders - each yields (properties_tuple, shapely_geom) -------- #

_repaired = 0


def _convert(geom, transform=None, simplify=None):
    def bump():
        global _repaired
        _repaired += 1
    return convert_geometry(geom, transform, on_repair=bump, simplify=simplify)


def us_counties(zpath):
    require_fields(zpath, ["STUSPS", "NAME"], "county name and state code are "
                                              "both required properties")
    for geom, rec in read_shapefile_zip(zpath):
        code = (rec["STUSPS"] or "").strip().upper()
        if len(code) != 2:
            continue                          # contract is exactly two letters
        g = _convert(geom)
        if g:
            # NAME ("Jefferson"), not NAMELSAD ("Jefferson Parish") - the short
            # form is what the operator tables store and match on, and what the
            # split-state section tables are keyed on.
            name = (rec["NAME"] or "").strip()
            yield (name, code, "US", section_for(name, code, "US")), g


def ca_counties(zpath):
    require_fields(zpath, ["PRUID", "CDNAME"], "census-division name and province "
                                               "code are both required")
    tf = ca_transformer()
    for geom, rec in read_shapefile_zip(zpath, encoding="latin-1"):
        code = PRUID_TO_PROV.get(str(rec["PRUID"]).strip())
        if not code:
            log(f"  WARNING: unmapped PRUID {rec['PRUID']!r} - skipped")
            continue
        g = _convert(geom, tf, simplify=CA_SIMPLIFY)
        if g:
            name = clean_name(rec["CDNAME"])
            yield (name, code, "CA", section_for(name, code, "CA")), g


# --- Phase 3 - build the table --------------------------------------------- #
# The work database and the published one get identical DDL, so publishing is a
# copy rather than a transform. {q} is the schema qualifier ("" or "lookup.").
# Statements are executed individually rather than as a script, because
# executescript() COMMITs any open transaction first and the publish needs to be
# a single unit.

# `id` is assigned US-first, so it doubles as the border tie-break ordering.
# The feature table has no geom column; geometry lives one polygon part per row
# in _parts, whose part_id is both its rowid and exactly _bbox.id.
DDL = [
    f"DROP TABLE IF EXISTS {{q}}{COUNTIES_TABLE}_bbox",
    f"DROP TABLE IF EXISTS {{q}}{COUNTIES_TABLE}_parts",
    f"DROP TABLE IF EXISTS {{q}}{COUNTIES_TABLE}",
    f"CREATE TABLE {{q}}{COUNTIES_TABLE} (id INTEGER PRIMARY KEY,"
    f" county TEXT, state TEXT NOT NULL, country TEXT NOT NULL,"
    f" arrl_section TEXT)",
    f"CREATE TABLE {{q}}{COUNTIES_TABLE}_parts ("
    f" part_id    INTEGER PRIMARY KEY,"
    f" feature_id INTEGER NOT NULL,"   # -> counties.id
    f" geom       BLOB NOT NULL)",     # WKB Polygon, WGS84 lon/lat
    f"CREATE VIRTUAL TABLE {{q}}{COUNTIES_TABLE}_bbox USING"
    f" rtree(id, minx, maxx, miny, maxy, +feature_id)",
    f"CREATE INDEX {{q}}idx_{COUNTIES_TABLE}_state_county"
    f" ON {COUNTIES_TABLE}(state, county)",
    # "which counties are in this section" is the reverse of the lookup above
    # and has no covering prefix in it.
    f"CREATE INDEX {{q}}idx_{COUNTIES_TABLE}_section"
    f" ON {COUNTIES_TABLE}(arrl_section)",
]

INSERT = ("INSERT INTO counties (id, county, state, country, arrl_section) "
          "VALUES (?,?,?,?,?)")

PART_INSERT = ("INSERT INTO counties_parts (part_id, feature_id, geom) "
               "VALUES (?,?,?)")

BBOX_INSERT = "INSERT INTO counties_bbox VALUES (?,?,?,?,?,?)"

# Applied AFTER the rows are in, by both build_table() and publish(). This index
# only serves feature reassembly; the lookup path joins on part_id, which is the
# rowid. Keeping it out of DDL avoids maintaining a 180,961-row index across
# every INSERT, which took publish from 107s to 6s. The other indexes stay in
# DDL - they cover 3,528 rows.
POST_DDL = ["CREATE INDEX {q}idx_counties_parts_feature "
            "ON counties_parts(feature_id)"]


def build_table(con, sources):
    """Build the table plus its R*Tree from an ordered list of feature sources.

    Source order IS id order, and the caller puts the US source first, so
    `ORDER BY (country='US') DESC, id` resolves a border point to the US shape.
    Streams, so peak memory is one geometry, not the ~300 MB collection."""
    global _repaired
    _repaired = 0
    for stmt in DDL:
        con.execute(stmt.format(q=""))

    bbox_rows, feat_rows, part_rows = [], [], []
    part_id = fid = 0
    counts = []
    # A single Canadian feature can take tens of seconds, so report on a timer
    # rather than sit silent for minutes and look like a hang.
    next_report = time.time() + 15.0
    for src in sources:
        n = 0
        for props, geom in src:
            fid += 1
            feat_rows.append((fid, *props))
            for part in (geom.geoms if geom.geom_type == "MultiPolygon"
                         else [geom]):
                minx, miny, maxx, maxy = part.bounds
                part_id += 1
                bbox_rows.append((part_id, minx, maxx, miny, maxy, fid))
                # Enumeration order is geom.geoms order, so ORDER BY part_id
                # reassembles the feature exactly.
                part_rows.append((part_id, fid, part.wkb))
                # Flushed INSIDE the part loop, unlike the other two batches:
                # these rows carry blobs, and one feature can be 62,546 parts,
                # which would otherwise buffer ~87 MB of WKB on top of the
                # geometry it was extracted from.
                if len(part_rows) >= 500:
                    con.executemany(PART_INSERT, part_rows)
                    part_rows = []
            n += 1
            if time.time() >= next_report:
                log(f"    ... {fid:,} features, {part_id:,} parts so far")
                next_report = time.time() + 15.0
            if len(feat_rows) >= 500:
                con.executemany(INSERT, feat_rows)
                feat_rows = []
            if len(bbox_rows) >= 50000:
                con.executemany(BBOX_INSERT, bbox_rows)
                bbox_rows = []
        counts.append(n)
    if feat_rows:
        con.executemany(INSERT, feat_rows)
    if part_rows:
        con.executemany(PART_INSERT, part_rows)
    if bbox_rows:
        con.executemany(BBOX_INSERT, bbox_rows)
    for stmt in POST_DDL:
        con.execute(stmt.format(q=""))
    con.commit()
    return counts, fid, part_id


def verify_table(con):
    """Every feature must have at least one part, every part a feature, and the
    bbox index and the geometry table must agree row for row.

    Each of these failures is silent at query time: a feature with no part is
    invisible to every lookup, and a bbox row whose part_id has no geometry
    makes the lookup join drop a candidate. All are index lookups -
    `_parts.feature_id` is indexed and `part_id` is its rowid."""
    table = COUNTIES_TABLE

    def count(sql):
        return con.execute(sql).fetchone()[0]

    n_feat = count(f"SELECT COUNT(*) FROM {table}")
    n_part = count(f"SELECT COUNT(*) FROM {table}_parts")
    n_box = count(f"SELECT COUNT(*) FROM {table}_bbox")
    log(f"  {table}: {n_feat:,} feature(s), {n_part:,} geometry part(s), "
        f"{n_box:,} bbox part(s)")

    problems = [
        (count(f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS "
               f"(SELECT 1 FROM {table}_parts p WHERE p.feature_id = t.id)"),
         "{n} feature(s) with NO part - unfindable"),
        (count(f"SELECT COUNT(*) FROM {table}_parts p WHERE NOT EXISTS "
               f"(SELECT 1 FROM {table} t WHERE t.id = p.feature_id)"),
         "{n} part(s) pointing at no feature"),
        (count(f"SELECT COUNT(*) FROM {table}_bbox b WHERE NOT EXISTS "
               f"(SELECT 1 FROM {table}_parts p WHERE p.part_id = b.id)"),
         "{n} bbox part(s) with no geometry row"),
        (count(f"SELECT COUNT(*) FROM {table}_parts WHERE LENGTH(geom) = 0"),
         "{n} part(s) with empty geometry"),
        (count(f"SELECT COUNT(*) FROM {table} WHERE LENGTH(state) <> 2"),
         "{n} row(s) whose state code is not two letters"),
        (n_part != n_box,
         f"bbox/geometry row count mismatch: {n_box:,} vs {n_part:,}"),
        (not n_feat, "table is EMPTY"),
    ]
    ok = True
    for bad, message in problems:
        if bad:
            ok = False
            log("    " + message.format(n=bad))
    return ok


def report_sections(con):
    """Log the section breakdown, and raise a banner over anything unmapped.

    Neither direction is fatal - a county with no section still answers every
    geographic question the table exists for - but both mean the section tables
    and the published boundaries have drifted apart, which is otherwise
    invisible: the column is simply NULL for counties nobody happens to query.

      * a county with NO section is a name the tables do not carry - a new or
        renamed county in a split state, or a new Ontario census division;
      * a name in the tables that NO county has is the same event seen from the
        other side, and is the one that silently leaves a real county unmapped
        under its new name.

    Only the split states and Ontario can produce either; everywhere else the
    section comes from the state code, which verify_table already constrains."""
    log("  ARRL/RAC section breakdown:")
    for sec, n in con.execute(
        f"SELECT COALESCE(arrl_section, '(none)'), COUNT(*) FROM "
        f"{COUNTIES_TABLE} GROUP BY 1 ORDER BY COUNT(*) DESC, 1"
    ):
        log(f"    {sec:>6}: {n:>5,}")

    unmapped = [f"{country}/{state}: {county}" for country, state, county in
                con.execute(f"SELECT country, state, county FROM "
                            f"{COUNTIES_TABLE} WHERE arrl_section IS NULL "
                            f"ORDER BY country, state, county")]

    unused = []
    for st, names in US_SECTION_BY_COUNTY.items():
        present = {r[0] for r in con.execute(
            f"SELECT county FROM {COUNTIES_TABLE} "
            f"WHERE country='US' AND state=?", (st,))}
        unused += [f"US/{st}: {n}" for n in sorted(set(names) - present)]
    present = {r[0] for r in con.execute(
        f"SELECT county FROM {COUNTIES_TABLE} "
        f"WHERE country='CA' AND state='ON'")}
    unused += [f"CA/ON: {n}" for n in sorted(set(CA_ON_SECTION_BY_CD) - present)]

    if not (unmapped or unused):
        return

    # The full lists, once, before the capped banner below.
    for u in unmapped:
        log(f"    no section: {u}")
    for u in unused:
        log(f"    unused name: {u}")

    # Capped: real drift is a handful of names, and this banner is replayed in
    # full at the end of the run. A structural change (a whole state renamed)
    # would otherwise bury every other notice twice over, and the count is the
    # part that tells you which it is - the full lists stay in the log above.
    def sample(names, limit=15):
        out = [f"     {n}" for n in names[:limit]]
        if len(names) > limit:
            out.append(f"     ... and {len(names) - limit} more")
        return out

    lines = [" NOTE: the section tables no longer match the boundaries", ""]
    if unmapped:
        lines += [f"   {len(unmapped)} county/counties with NO section "
                  f"(arrl_section stays NULL):"]
        lines += sample(unmapped)
    if unused:
        if unmapped:
            lines.append("")
        lines += [f"   {len(unused)} name(s) in the section tables that no "
                  f"county has:"]
        lines += sample(unused)
    lines += ["", "   Both mean a county was renamed, added or resplit "
                  "upstream.",
              "   Update SPLIT_SECTIONS / ON_SECTION_BY_CD in sections.py,",
              "   which importer_fcc.py and importer_ca.py read too - so one",
              "   edit there covers all three importers."]
    log_banner(lines)


# --- Phase 4 - publish ----------------------------------------------------- #

def publish(con, final_db):
    """Copy the feature table, the geometry table and the R*Tree into
    lookup_data.sqlite.

    All three are replaced inside ONE transaction: a lookup joins all three, so
    a mismatched trio does not error, it returns wrong geometry. R*Tree DDL is
    transactional (its shadow tables are ordinary tables), so a ROLLBACK undoes
    the virtual tables too. Other importers' tables in the same file are not
    read, written or dropped."""
    table = COUNTIES_TABLE
    con.isolation_level = None            # the only transaction is the one below
    con.execute("ATTACH DATABASE ? AS lookup", (final_db,))
    try:
        existed = con.execute(
            "SELECT COUNT(*) FROM lookup.sqlite_master "
            "WHERE type='table' AND name = ?", (table,)).fetchone()[0]
        log(f"{'Replacing' if existed else 'Creating'} {table} in "
            f"{os.path.basename(final_db)} ...")

        con.execute("BEGIN IMMEDIATE")
        for stmt in DDL:
            con.execute(stmt.format(q="lookup."))
        con.execute(f"INSERT INTO lookup.{table} SELECT * FROM main.{table}")
        con.execute(f"INSERT INTO lookup.{table}_parts "
                    f"SELECT part_id, feature_id, geom "
                    f"FROM main.{table}_parts")
        con.execute(f"INSERT INTO lookup.{table}_bbox "
                    f"SELECT id, minx, maxx, miny, maxy, feature_id "
                    f"FROM main.{table}_bbox")
        for stmt in POST_DDL:
            con.execute(stmt.format(q="lookup."))
        n = con.execute(f"SELECT COUNT(*) FROM lookup.{table}").fetchone()[0]
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("DETACH DATABASE lookup")
        raise
    con.execute("DETACH DATABASE lookup")
    log(f"  {table}: {n:,} rows published")
    log(f"{'Replaced' if existed else 'Created'} the boundary table in "
        f"{final_db}{' (previous version discarded)' if existed else ''}")


# --- Preflight ------------------------------------------------------------- #

def preflight():
    """Abort before anything is downloaded if a package is missing. Also proves
    this SQLite build has R*Tree compiled in - the whole schema rests on it, so
    finding out here beats finding out after a ~10 minute build."""
    pip_names = {"shapefile": "pyshp", "shapely": "shapely", "pyproj": "pyproj",
                 "requests": "requests"}
    missing = []
    for mod in ("requests", "shapefile", "shapely", "pyproj"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_names[mod])
    if missing:
        sys.exit(
            "ERROR: missing required package(s):\n"
            f"  {', '.join(sorted(missing))}\n"
            "\nInstall them:\n"
            f"  python -m pip install {' '.join(sorted(missing))}\n"
            "  (or: python -m pip install -r requirements.txt)")

    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING "
                    "rtree(id, minx, maxx, miny, maxy, +aux)")
    except sqlite3.OperationalError as e:
        sys.exit(f"ERROR: this SQLite build has no R*Tree support ({e}).\n"
                 f"  sqlite3 {sqlite3.sqlite_version}; the boundary table "
                 f"cannot be indexed without it.")
    finally:
        con.close()


# --- Main ------------------------------------------------------------------ #

def build_parser():
    return argparse.ArgumentParser(
        prog="importer_boundaries.py",
        description="Build the `counties` boundary table (plus its R*Tree "
                    "index) in lookup_data.sqlite from the newest published "
                    "Census and StatCan releases. Takes no options: every path "
                    "is fixed under the project root.")


def run():
    """Run the whole import. run_importers.py calls this directly, and raises
    SystemExit on failure for the menu to report."""
    global _log_fh

    preflight()

    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None
    _notices.clear()

    for d in (DOWNLOADS_DIR, CACHES_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)

    _log_fh = open(os.path.join(LOGS_DIR, RUN_LOG), "w", encoding="utf-8")
    t0 = time.time()
    log(f"=== Boundary import started ({time.strftime('%Y-%m-%d')}) ===")
    log(f"Building into {WORK_DB}")
    log(f"  -> becomes the {COUNTIES_TABLE} table of {DB_PATH} on success")

    log("--- Phase 1: cleanup ---")
    cleanup_old_data()

    log("--- Phase 2: resolve + download sources ---")
    us_v, us_county_zip = resolve_us()
    ca_y, ca_cd_zip = resolve_ca()
    log(f"  US:     Census cartographic boundaries, vintage {us_v}")
    log(f"  Canada: StatCan {ca_y} census boundary file")

    # try/finally, not a bare close() at the end: run_importers.py returns to
    # its menu on failure, and on Windows a connection leaked by a raising
    # phase makes the NEXT run's cleanup of WORK_DB fail outright.
    con = sqlite3.connect(WORK_DB)
    try:
        con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")

        # US source first: that fixes id order, which is the border tie-break.
        log("--- Phase 3: build counties (US counties, then CA census "
            "divisions) ---")
        (n_us, n_ca), feats, parts = build_table(
            con, [us_counties(us_county_zip), ca_counties(ca_cd_zip)])
        log(f"  {n_us} US + {n_ca} CA = {feats} features, "
            f"{parts:,} bbox parts")
        if _repaired:
            log(f"  {_repaired} geometry(ies) repaired by make_valid")
        ok = verify_table(con)
        report_sections(con)

        if not ok:
            sys.exit("ERROR: build verification FAILED - aborting before "
                     "publish. The failed build is left at "
                     f"{WORK_DB} for inspection and is NOT published; the "
                     "previously published table is untouched.")

        log("--- Phase 4: publish ---")
        # No VACUUM: it would rewrite the whole ~200 MB work database, which the
        # next run deletes anyway, and publish() copies rows into freshly
        # created tables, so the published result is already compact.
        publish(con, DB_PATH)
    finally:
        con.close()

    log(f"=== SUCCESS: {COUNTIES_TABLE} in {DB_PATH} "
        f"in {(time.time() - t0) / 60:,.1f} minutes ===")
    replay_notices()
    _log_fh.close()
    _log_fh = None


def main():
    build_parser().parse_args()
    run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user (Ctrl-C). The published table is untouched - "
            "the work database is cleaned up by the next run.")
        try:
            if _log_fh:
                _log_fh.flush()
                _log_fh.close()
        except Exception:
            pass
        sys.exit(130)
