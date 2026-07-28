#!/usr/bin/env python3
r"""
importer_zones.py - build cq_zones, itu_zones and dxcc_entities, with their
per-part geometry tables and R*Tree indexes, in lookup_data.sqlite.

Sources track each repo's default branch and are cached in downloads/. Every run
sends the stored ETag (caches/zones_sources.json) as If-None-Match: 304 keeps the
file on disk, 200 replaces it, a failure keeps what is there and says so. Neither
repo publishes usable releases, so freshness is asked of HTTP, not of a tag. No
file is hash-pinned, so structurally_ok() and the per-table verification are the
whole of what a download must pass.

Schema - the per-part spatial pattern, as in states/counties. See
HANDOFF-spatial-schema.md:
    cq_zones      (id, zone, name, label_lat, label_lon, area_deg2)     40
    itu_zones     (id, zone, name, label_lat, label_lon, area_deg2)     90
    dxcc_entities (id, prefix, name, entity_code, area_deg2)           341
    <table>_parts (part_id INTEGER PRIMARY KEY, feature_id, geom)
    <table>_bbox  rtree(id, minx, maxx, miny, maxy, +feature_id)

part_id IS _bbox.id for the same polygon, so the lookup join is a rowid seek.
geom is WKB Polygon (never MultiPolygon), WGS84 lon/lat. Query the bbox, then
confirm with wkb.loads(geom).covers(point) and take the first hit: the R*Tree is
a prefilter with false positives, never false negatives.

Five things a query will get wrong:
 1. The zones overlap AND leave gaps - not a partition, so a point in an overlap
    has no correct answer, only a deterministic one. Pairs are logged.
 2. `id` IS that tie-break: CQ/ITU ascend by zone number (lower wins), DXCC by
    AREA so enclaves beat hosts - (12.4534, 41.9029) is HV, not I. Never ORDER
    BY entity_code.
 3. Some CQ/ITU zones are drawn past +/-180, out to -205; parts_of() splits and
    shifts them back, or a point at 175E silently misses them. The only stored
    geometry that is a translation of upstream's.
 4. itu_zones.name is a packed display string, not a name - '!!!:PY:**Brazil,
    north of 16.5 S...#PY0:**Fernando ...'. Stored verbatim; cleaning it is a
    guess. cq_zones.name is a real name.
 5. dxcc_entities has 341 rows for 340 entities - Conway Reef (3D2) is two
    features sharing code 489 - so neither index is UNIQUE.

DXCC_CORRECTIONS re-applies four audited upstream errors. Upstream geometry is
generalised (~16,600 vertices worldwide) and Vanuatu and Palestine are
incomplete; nothing here repairs that.

Usage: .venv\Scripts\python importer_zones.py [--no-download]
(or entry 6 of run_importers.py). Requires requests and shapely.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

# --- Constants -------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(HERE, "downloads")
CACHES_DIR = os.path.join(HERE, "caches")
LOGS_DIR = os.path.join(HERE, "logs")
DB_PATH = os.path.join(HERE, "lookup_data.sqlite")
WORK_DB = os.path.join(CACHES_DIR, "zones_work.sqlite")
RUN_LOG = "zones_run.log"

# Losing this costs one re-download per source and nothing else.
SOURCE_STATE = os.path.join(CACHES_DIR, "zones_sources.json")

CQ_TABLE, ITU_TABLE, DXCC_TABLE = "cq_zones", "itu_zones", "dxcc_entities"
TABLES = (CQ_TABLE, ITU_TABLE, DXCC_TABLE)

HTTP_HEADERS = {"User-Agent": "zones-import/1.0 (+lookup_data build)"}
HTTP_TIMEOUT = (30, 300)          # (connect, read); the largest file is 2.8 MB

HB9HIL = "https://raw.githubusercontent.com/HB9HIL/hamradio-zones-geojson/main/"
F6FVY = "https://raw.githubusercontent.com/f6fvy/dxcc_map/main/"

UNTOUCHED = ("Nothing was changed; the previously published tables are "
             "untouched.")

ZONE_COLS = ("zone INTEGER NOT NULL", "name TEXT", "label_lat REAL",
             "label_lon REAL", "area_deg2 REAL")

# One entry per table: where the file comes from, what it must contain, and the
# columns it becomes. `features` is the count upstream is expected to hold; the
# ref moves, so a mismatch raises a banner rather than aborting. `props` are the
# keys the first feature must carry for the file to be the right one.
def _zone_spec(kind, table, filename, features):
    return {
        "label": f"{kind.upper()} zones", "url": HB9HIL + filename,
        "file": filename, "features": features, "cols": ZONE_COLS,
        "props": tuple(f"{kind}_zone_{s}" for s in ("number", "name",
                                                    "name_loc")),
        "indexes": (f"CREATE UNIQUE INDEX {{q}}idx_{table}_zone "
                    f"ON {table}(zone)",),
    }


SOURCES = {
    CQ_TABLE: _zone_spec("cq", CQ_TABLE, "cqzones.geojson", 40),
    ITU_TABLE: _zone_spec("itu", ITU_TABLE, "ituzones.geojson", 90),
    DXCC_TABLE: {
        "label": "DXCC entities", "url": F6FVY + "dxcc.geojson",
        "file": "dxcc.geojson", "features": 341,
        "cols": ("prefix TEXT NOT NULL", "name TEXT", "entity_code INTEGER",
                 "area_deg2 REAL"),
        "props": ("dxcc_prefix", "dxcc_name", "dxcc_entity_code"),
        # Not UNIQUE: Conway Reef is two features sharing 3D2 / code 489.
        "indexes": ("CREATE INDEX {q}idx_dxcc_entities_prefix "
                    "ON dxcc_entities(prefix)",
                    "CREATE INDEX {q}idx_dxcc_entities_code "
                    "ON dxcc_entities(entity_code)"),
    },
}

# Below this a file is truncated or simply the wrong file; it aborts the run.
MIN_FEATURES = 20

# (prefix, field, wrong value, corrected value, why).
DXCC_CORRECTIONS = [
    ("3A", "entity_code", 206, 260, "206 is Austria"),
    ("9M2", "entity_code", 229, 299, "229 is the deleted entity East Germany"),
    ("FT/z", "entity_code", 131, 10, "131 is Kerguelen Is."),
    ("TU", "name", "Cote de'Ivoire", "Cote d'Ivoire", "upstream typo"),
]

# Borders digitised from the same line disagree in the last bit; slivers of
# ~1e-9 deg^2 are touching, not overlapping.
OVERLAP_EPSILON = 1e-7

# WORLD_EPS keeps arithmetic noise from counting as out-of-range: Fiji,
# Antarctica and Asiatic Russia are cut at the dateline upstream and reach
# 180.00000000000014, ~15 nanometres past it. 1e-9 deg is ~0.1 mm; genuinely
# out-of-range features reach -205. MIN_SPLIT_AREA drops what the clip leaves
# behind at the seam.
WORLD_EPS = 1e-9
MIN_SPLIT_AREA = 1e-12

# --- Logging (console + utf-8 log file) ------------------------------------

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


# --- Preflight -------------------------------------------------------------

def preflight():
    """Abort before anything is downloaded if a package or R*Tree is missing."""
    missing = []
    for name in ("requests", "shapely"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        sys.exit(f"ERROR: missing required package(s): {', '.join(missing)}\n"
                 f"  python -m pip install {' '.join(missing)}\n"
                 f"  (or: python -m pip install -r requirements.txt)")

    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING "
                    "rtree(id, minx, maxx, miny, maxy, +aux)")
    except sqlite3.OperationalError as e:
        sys.exit(f"ERROR: this SQLite build has no R*Tree support ({e}).\n"
                 f"  sqlite3 {sqlite3.sqlite_version}; the zone tables cannot "
                 f"be indexed without it.")
    finally:
        con.close()


# --- Phase 1: cleanup ------------------------------------------------------

def cleanup_old_data():
    """Delete what a previous run stranded.

    Never touches lookup_data.sqlite, the installed GeoJSON or SOURCE_STATE, so
    a run that dies leaves the published tables and the ETags consistent.
    """
    victims = [WORK_DB, WORK_DB + "-journal", SOURCE_STATE + ".part"]
    victims += [os.path.join(DOWNLOADS_DIR, s["file"] + ".part")
                for s in SOURCES.values()]
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
    log(f"Cleanup: {removed} stale file(s) removed. The published tables and "
        f"the installed GeoJSON files stay in place until replaced.")


# --- Phase 2: resolve the three sources ------------------------------------

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state():
    """The recorded ETag/Last-Modified per file. Absent or corrupt -> {}."""
    try:
        with open(SOURCE_STATE, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state):
    """Write SOURCE_STATE atomically. Failure costs a re-download, not a run."""
    tmp = SOURCE_STATE + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, SOURCE_STATE)
    except OSError as e:
        log(f"  could not write {os.path.basename(SOURCE_STATE)} ({e}) - the "
            f"next run will re-download instead of asking for a 304")


def structurally_ok(path, spec):
    """Raise ValueError unless `path` is the FeatureCollection we expect."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"not readable as JSON ({e})")
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
        raise ValueError("not a GeoJSON FeatureCollection")
    feats = doc.get("features")
    if not isinstance(feats, list) or len(feats) < MIN_FEATURES:
        n = len(feats) if isinstance(feats, list) else 0
        raise ValueError(f"only {n} feature(s), expected at least {MIN_FEATURES}")
    missing = [k for k in spec["props"]
               if k not in (feats[0].get("properties") or {})]
    if missing:
        raise ValueError(f"first feature is missing {', '.join(missing)}")


def _keep_existing(path, spec, why, provenance, banner=False):
    """Reuse downloads/<file>, or abort saying why there is nothing to reuse.

    A file that is present but unusable is a hard error, not a silent
    re-download: the only ways to get one are a truncated write or an edit.
    """
    label = spec["label"]
    if not os.path.exists(path):
        sys.exit(f"ERROR: {label}: {why}, and there is no copy at {path} to "
                 f"fall back on. {UNTOUCHED}")
    try:
        structurally_ok(path, spec)
    except ValueError as e:
        sys.exit(f"ERROR: {label}: {why}, and downloads/{spec['file']} is "
                 f"unusable ({e}). Delete it and re-run to force a fresh "
                 f"download. {UNTOUCHED}")
    if banner:
        log_banner([
            f" {label}: {why}, so this run reuses the copy already in",
            f"   downloads/{spec['file']}",
            f" It passed its structure check, but it is whatever the last",
            f" successful download left there - not known to be current.",
        ])
    else:
        log(f"{label}: keeping downloads/{spec['file']} ({why})")
    return path, provenance


def fetch_source(spec, no_download, state):
    """Return (path, provenance) for one source, updating `state` in place.

    One conditional GET decides everything: 304 reuses the file on disk, 200
    streams to <file>.part and renames it over the old copy only after the
    structure check, anything else keeps what is there.
    """
    path = os.path.join(DOWNLOADS_DIR, spec["file"])
    label = spec["label"]
    have = os.path.exists(path)
    entry = state.get(spec["file"]) or {}

    if no_download:
        return _keep_existing(path, spec, "--no-download",
                              "downloads/ (--no-download)")

    # Offered only when the file they describe exists, or an ETag would earn a
    # 304 for a file that is not there.
    headers = dict(HTTP_HEADERS)
    if have and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    if have and entry.get("last_modified"):
        headers["If-Modified-Since"] = entry["last_modified"]

    tmp = path + ".part"
    log(f"{label}: checking {spec['url']}")
    fresh = None
    try:
        import requests
        with requests.get(spec["url"], timeout=HTTP_TIMEOUT, stream=True,
                          headers=headers) as r:
            if r.status_code != 304:
                r.raise_for_status()
                h = hashlib.sha256()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        h.update(chunk)
                        f.write(chunk)
                fresh = {"sha256": h.hexdigest(),
                         "etag": r.headers.get("ETag"),
                         "last_modified": r.headers.get("Last-Modified")}
        if fresh is not None:
            structurally_ok(tmp, spec)
    except Exception as e:                # requests, OSError, ValueError alike
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        log(f"  download failed: {e}")
        return _keep_existing(path, spec, "the download failed",
                              "downloads/ (stale - download failed)",
                              banner=True)

    if fresh is None:
        return _keep_existing(path, spec, "unchanged upstream (HTTP 304)",
                              "downloads/ (304 - unchanged upstream)")

    # With no recorded digest, hash what is on disk rather than assume the
    # worst: guessing fires the "upstream CHANGED" banner on an unchanged file.
    prior = entry.get("sha256")
    if have and prior is None:
        try:
            prior = sha256_of(path)
        except OSError:
            prior = None
    same = have and fresh["sha256"] == prior
    os.replace(tmp, path)
    state[spec["file"]] = fresh
    size = os.path.getsize(path) / 1e6
    if same:
        log(f"{label}: {size:,.1f} MB re-fetched, byte-identical to the "
            f"previous copy")
        return path, "downloaded (unchanged)"
    log(f"{label}: {size:,.1f} MB -> downloads/{spec['file']} "
        f"({'UPDATED - upstream changed' if have else 'first download'})")
    if have:
        log_banner([
            f" {label}: upstream has CHANGED and the new file is now in use.",
            f"   downloads/{spec['file']}  ({size:,.1f} MB)",
            f" The counts and geometry checks below apply to the new data. The",
            f" DXCC corrections and the documented caveats were audited against",
            f" the previous file - re-check anything reported as unexpected.",
        ])
    return path, "downloaded (updated)"


# --- Geometry: repair, and the antimeridian split --------------------------

_repaired = 0
_wrapped = 0


def _polygons(g):
    """Every Polygon inside `g`, flattened, empties dropped.

    make_valid can return a GeometryCollection with stray lines where a ring
    pinched; only the polygonal parts are a zone.
    """
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [q for part in g.geoms for q in _polygons(part)]
    return []


def repair(g):
    """Make an invalid Polygon/MultiPolygon valid, one part at a time.

    GEOS `covers` against a self-intersecting ring can return the wrong answer
    outright - a silently incorrect zone rather than an error. The re-check at
    the end matters: per-part repair cannot fix parts that overlap EACH OTHER,
    so a still-invalid result falls back to the union.
    """
    global _repaired
    import shapely
    from shapely.geometry import MultiPolygon
    from shapely.validation import make_valid

    was_polygon = g.geom_type == "Polygon"
    parts = [g] if was_polygon else list(g.geoms)

    out, n_bad = [], 0
    for part, ok in zip(parts, shapely.is_valid(parts)):
        if ok:
            out.append(part)
        else:
            n_bad += 1
            out.extend(_polygons(make_valid(part)))
    if not out:
        return None
    if n_bad:
        _repaired += 1

    fixed = out[0] if (was_polygon and len(out) == 1) else MultiPolygon(out)
    if fixed.is_valid:
        return fixed
    from shapely.ops import unary_union
    log("    (parts overlap - falling back to whole-geometry union)")
    if not n_bad:
        _repaired += 1
    return unary_union(_polygons(make_valid(fixed)))


def parts_of(geom_json, what):
    """One GeoJSON geometry -> the WGS84 Polygons to store for it.

    Repairs if invalid, splits at +/-180 (each side its own part, far side
    shifted into range), flattens to Polygons because one part is one row.
    Anything already in range passes through untouched.
    """
    global _wrapped
    from shapely.affinity import translate
    from shapely.geometry import box, shape

    if not geom_json or not geom_json.get("coordinates"):
        return []
    if geom_json.get("type") not in ("Polygon", "MultiPolygon"):
        log(f"  WARNING: {what} has geometry type {geom_json.get('type')!r} "
            f"- skipped")
        return []

    g = shape(geom_json)
    if not g.is_valid:
        g = repair(g)
    if g is None or g.is_empty:
        return []

    minx, _, maxx, _ = g.bounds
    if minx >= -180 - WORLD_EPS and maxx <= 180 + WORLD_EPS:
        return _polygons(g)

    world = box(-180, -90, 180, 90)
    out = []
    for shift in (-360, 0, 360):
        h = g if shift == 0 else translate(g, xoff=shift)
        if h.bounds[0] > 180 or h.bounds[2] < -180:
            continue
        out.extend(p for p in _polygons(h.intersection(world))
                   if p.area > MIN_SPLIT_AREA)
    if out:
        _wrapped += 1
    return out


# --- Loading features ------------------------------------------------------
#
# Every feature is materialised before anything is written, which is what lets
# dxcc_entities be ordered by polygon area: that tie-break cannot be known until
# every feature has been measured.

class Feature:
    """One row-to-be: its attribute tuple, its polygons, their total area."""
    __slots__ = ("props", "parts", "area")

    def __init__(self, props, parts):
        self.props = props
        self.parts = parts
        self.area = sum(p.area for p in parts)


def _label_lat_lon(value, what):
    """`*_zone_name_loc` is [lat, lon] - the opposite order to the geometry."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        log(f"  WARNING: {what} has an unusable label location {value!r}")
        return None, None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        log(f"  WARNING: {what} has a non-numeric label location {value!r}")
        return None, None


def load_zones(doc, kind):
    """CQ/ITU -> [Feature((zone, name, label_lat, label_lon), parts)]."""
    num_key, name_key = f"{kind}_zone_number", f"{kind}_zone_name"
    loc_key, tag = f"{kind}_zone_name_loc", kind.upper()

    out = []
    for i, feat in enumerate(doc["features"], start=1):
        props = feat.get("properties") or {}
        try:
            zone = int(props[num_key])
        except (KeyError, TypeError, ValueError):
            # A zone that cannot be numbered cannot be looked up or joined.
            log(f"  WARNING: {tag} feature {i} has no usable {num_key} "
                f"({props.get(num_key)!r}) - skipped")
            continue
        lat, lon = _label_lat_lon(props.get(loc_key), f"{tag} zone {zone}")
        parts = parts_of(feat.get("geometry"), f"{tag} zone {zone}")
        if not parts:
            log(f"  WARNING: {tag} zone {zone} has no usable geometry - skipped")
            continue
        out.append(Feature((zone, props.get(name_key) or None, lat, lon), parts))
    out.sort(key=lambda f: f.props[0])          # id order = zone number
    return out


def load_dxcc(doc):
    """DXCC -> [Feature((prefix, name, entity_code), parts)], smallest first."""
    out = []
    for i, feat in enumerate(doc["features"], start=1):
        props = feat.get("properties") or {}
        prefix = (props.get("dxcc_prefix") or "").strip()
        if not prefix:
            log(f"  WARNING: DXCC feature {i} has no dxcc_prefix - skipped")
            continue
        try:
            code = int(props["dxcc_entity_code"])
        except (KeyError, TypeError, ValueError):
            log(f"  WARNING: DXCC {prefix} has no usable dxcc_entity_code "
                f"({props.get('dxcc_entity_code')!r}) - stored as NULL")
            code = None
        parts = parts_of(feat.get("geometry"), f"DXCC {prefix}")
        if not parts:
            log(f"  WARNING: DXCC {prefix} has no usable geometry - skipped")
            continue
        out.append(Feature((prefix, (props.get("dxcc_name") or "").strip()
                            or None, code), parts))

    apply_dxcc_corrections(out)
    # id order = area ascending, so an enclave is tested before its host.
    out.sort(key=lambda f: (f.area, f.props[0]))
    return out


def apply_dxcc_corrections(feats):
    """Re-apply the audited entity-code fixes; see DXCC_CORRECTIONS.

    A correction matching neither the wrong nor the right value means the source
    moved under this list, so it gets a banner rather than a silent skip.
    """
    field_index = {"prefix": 0, "name": 1, "entity_code": 2}
    applied, already, stale = [], [], []
    for prefix, field, wrong, right, why in DXCC_CORRECTIONS:
        idx = field_index[field]
        hits = [f for f in feats if f.props[0] == prefix]
        if not hits:
            stale.append(f"   {prefix:<5} {field}: no feature with this prefix")
            continue
        for f in hits:
            current = f.props[idx]
            if current == wrong:
                props = list(f.props)
                props[idx] = right
                f.props = tuple(props)
                applied.append(f"   {prefix:<5} {field} {wrong!r} -> {right!r} "
                               f"({why})")
            elif current == right:
                already.append(f"{prefix} {field}")
            else:
                stale.append(f"   {prefix:<5} {field}: found {current!r}, "
                             f"expected {wrong!r} or {right!r}")
    if applied:
        log(f"  {len(applied)} DXCC correction(s) applied:")
        for line in applied:
            log(line)
    if already:
        log(f"  {len(already)} DXCC correction(s) already present in the "
            f"source: {', '.join(already)}")
    if stale:
        log_banner([
            " DXCC CORRECTIONS DO NOT MATCH THE SOURCE:",
            *stale,
            " The source has changed under DXCC_CORRECTIONS. The affected",
            " entities are stored with whatever the source says; re-check them",
            " against the ARRL/ADIF entity list before trusting them.",
        ])


# --- Phases 3-5: build the tables ------------------------------------------
#
# The work database and the published one get identical DDL, so publishing is a
# copy rather than a transform; {q} is the schema qualifier ("" or "lookup.").
# Statements run one at a time because executescript() would COMMIT the publish
# transaction out from under itself.

def ddl_for(table):
    """Drop and recreate the three objects of the per-part spatial pattern."""
    spec = SOURCES[table]
    return [
        f"DROP TABLE IF EXISTS {{q}}{table}_bbox",
        f"DROP TABLE IF EXISTS {{q}}{table}_parts",
        f"DROP TABLE IF EXISTS {{q}}{table}",
        f"CREATE TABLE {{q}}{table} (id INTEGER PRIMARY KEY, "
        + ", ".join(spec["cols"]) + ")",
        # part_id is deliberately the rowid AND exactly {table}_bbox.id.
        f"CREATE TABLE {{q}}{table}_parts (part_id INTEGER PRIMARY KEY,"
        f" feature_id INTEGER NOT NULL, geom BLOB NOT NULL)",
        f"CREATE VIRTUAL TABLE {{q}}{table}_bbox USING"
        f" rtree(id, minx, maxx, miny, maxy, +feature_id)",
        *spec["indexes"],
    ]


# Applied after the rows are in, by both build_table() and publish(). Only for
# reassembling a whole feature; the lookup path joins on part_id, the rowid.
POST_INDEX = "CREATE INDEX {q}idx_{t}_parts_feature ON {t}_parts(feature_id)"


def insert_for(table):
    cols = SOURCES[table]["cols"]
    names = ", ".join(c.split()[0] for c in cols)
    return (f"INSERT INTO {table} (id, {names}) "
            f"VALUES ({','.join('?' * (len(cols) + 1))})")


def build_table(con, table, feats):
    """Build one table, its geometry table and its R*Tree. List order IS id
    order - the loaders sorted for the tie-break each table documents."""
    for stmt in ddl_for(table):
        con.execute(stmt.format(q=""))

    feat_rows, part_rows, bbox_rows = [], [], []
    part_id = 0
    for fid, f in enumerate(feats, start=1):
        feat_rows.append((fid, *f.props, f.area))
        for part in f.parts:
            part_id += 1
            minx, miny, maxx, maxy = part.bounds
            part_rows.append((part_id, fid, part.wkb))
            bbox_rows.append((part_id, minx, maxx, miny, maxy, fid))

    con.executemany(insert_for(table), feat_rows)
    con.executemany(f"INSERT INTO {table}_parts (part_id, feature_id, geom) "
                    f"VALUES (?,?,?)", part_rows)
    con.executemany(f"INSERT INTO {table}_bbox VALUES (?,?,?,?,?,?)", bbox_rows)
    con.execute(POST_INDEX.format(q="", t=table))
    con.commit()
    return len(feat_rows), part_id


# --- Verification ----------------------------------------------------------

def verify_table(con, table, expected_features):
    """Check the integrity properties that otherwise fail SILENTLY at query time.

    A feature with no part is invisible to every lookup, a part pointing at no
    feature drops out of the join, and a bbox row with no geometry makes the join
    drop a candidate while still returning an answer. Checked against `_parts`,
    never the R*Tree, where feature_id is an unindexable aux column.
    """
    t = table
    q = lambda sql: con.execute(sql).fetchone()[0]

    n_feat, n_part, n_box = (q(f"SELECT COUNT(*) FROM {t}"),
                             q(f"SELECT COUNT(*) FROM {t}_parts"),
                             q(f"SELECT COUNT(*) FROM {t}_bbox"))
    log(f"  {t}: {n_feat:,} feature(s), {n_part:,} geometry part(s), "
        f"{n_box:,} bbox part(s)")
    if not n_feat:
        log("    table is EMPTY")
        return False

    ok = True
    if n_feat < MIN_FEATURES:
        ok = False
        log(f"    only {n_feat} feature(s), expected at least {MIN_FEATURES}")
    if n_part != n_box:
        ok = False
        log(f"    bbox/geometry row count mismatch: {n_box:,} vs {n_part:,}")

    # The out-of-range slack is 1e-4 because the R*Tree rounds its 32-bit bounds
    # OUTWARD - one float32 step at 180 is 1.5e-5 deg, so a part at
    # 180.00000000000014 reads back as 180.0000153. Anything genuinely
    # undersplit is out at 185 or -205.
    for sql, msg in (
        (f"SELECT COUNT(*) FROM {t} x WHERE NOT EXISTS (SELECT 1 FROM "
         f"{t}_parts p WHERE p.feature_id = x.id)",
         "feature(s) with NO part - unfindable"),
        (f"SELECT COUNT(*) FROM {t}_parts p WHERE NOT EXISTS (SELECT 1 FROM "
         f"{t} x WHERE x.id = p.feature_id)",
         "part(s) pointing at no feature"),
        (f"SELECT COUNT(*) FROM {t}_bbox b WHERE NOT EXISTS (SELECT 1 FROM "
         f"{t}_parts p WHERE p.part_id = b.id)",
         "bbox part(s) with no geometry row"),
        (f"SELECT COUNT(*) FROM {t}_parts WHERE LENGTH(geom) = 0",
         "part(s) with empty geometry"),
        (f"SELECT COUNT(*) FROM {t}_bbox WHERE minx < -180.0001 OR "
         f"maxx > 180.0001 OR miny < -90.0001 OR maxy > 90.0001",
         "bbox part(s) outside [-180,180]x[-90,90] - the antimeridian split "
         "did not happen"),
    ):
        n = q(sql)
        if n:
            ok = False
            log(f"    {n} {msg}")

    if n_feat != expected_features:
        # Not fatal - the file passed every other check - but this importer and
        # its source no longer agree about what is in it.
        log_banner([
            f" {t}: {n_feat} features, but SOURCES expects {expected_features}.",
            f" Either upstream changed or features were skipped - the warnings",
            f" above say which. The table is published as built; if upstream",
            f" changed, update the count in SOURCES once you have checked why.",
        ])
    return ok


def verify_zone_numbers(con, table, expected):
    """CQ/ITU: zone numbers must be 1..N, each exactly once. Caught here because
    the UNIQUE index would abort with an IntegrityError naming nothing."""
    ok = True
    dups = con.execute(f"SELECT zone, COUNT(*) FROM {table} GROUP BY zone "
                       f"HAVING COUNT(*) > 1 ORDER BY 1").fetchall()
    if dups:
        ok = False
        log(f"    DUPLICATE zone number(s): "
            f"{', '.join(f'{z} x{c}' for z, c in dups)}")
    have = {r[0] for r in con.execute(f"SELECT zone FROM {table}")}
    missing = sorted(set(range(1, expected + 1)) - have)
    extra = sorted(have - set(range(1, expected + 1)))
    if missing:
        ok = False
        log(f"    missing zone number(s): {missing}")
    if extra:
        log_banner([
            f" {table}: zone number(s) outside 1-{expected}: {extra}",
            f" The zone numbering is a closed set; this is worth a look.",
        ])
    return ok


def verify_dxcc(con):
    """DXCC-specific counts, and the one duplicate that is expected."""
    ok = True
    n, codes, no_code, no_name = con.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT entity_code), "
        f"SUM(entity_code IS NULL), SUM(name IS NULL) "
        f"FROM {DXCC_TABLE}").fetchone()
    log(f"    {n} feature(s), {codes} distinct entity code(s), "
        f"{no_code} without a code, {no_name} without a name")
    if no_code:
        ok = False
        log(f"    {no_code} entity(ies) with no entity_code - unjoinable to any "
            f"log or callbook")
    dups = con.execute(
        f"SELECT entity_code, COUNT(*) c, GROUP_CONCAT(prefix) FROM {DXCC_TABLE} "
        f"GROUP BY entity_code HAVING c > 1 ORDER BY c DESC, 1").fetchall()
    if dups:
        # 3D2 (Conway Reef) is expected; reported not asserted, because the
        # right response to a NEW one is to look.
        log(f"    {len(dups)} entity code(s) on more than one feature: "
            + "; ".join(f"{c} x{n_} ({p})" for c, n_, p in dups))
    return ok


def report_overlaps(con, table, key_sql):
    """List the feature pairs that genuinely overlap, worst first.

    A point inside an overlap has no correct answer, only the one ORDER BY id
    picks, so say where that happens. Runs against the stored geometry, so it
    covers the antimeridian split too.
    """
    from shapely import STRtree, wkb
    from shapely.geometry import MultiPolygon

    geoms, keys = [], []
    for fid, key in con.execute(f"SELECT id, {key_sql} FROM {table} ORDER BY id"):
        polys = [wkb.loads(b) for (b,) in con.execute(
            f"SELECT geom FROM {table}_parts WHERE feature_id = ? "
            f"ORDER BY part_id", (fid,))]
        if not polys:
            continue
        geoms.append(polys[0] if len(polys) == 1 else MultiPolygon(polys))
        keys.append(key)

    tree = STRtree(geoms)
    pairs = []
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects"):
            if i >= j:
                continue
            area = g.intersection(geoms[j]).area
            if area > OVERLAP_EPSILON:
                pairs.append((area, keys[i], keys[j]))
    pairs.sort(reverse=True)
    total = sum(g.area for g in geoms)
    log(f"    {total:,.0f} of 64,800 square degrees covered "
        f"({total / 648:.1f}% of the globe); {len(pairs)} overlapping pair(s)")
    for area, a, b in pairs[:10]:
        log(f"      {a} / {b}: {area:,.3f} deg^2")
    if len(pairs) > 10:
        log(f"      ... and {len(pairs) - 10} more, all smaller")


# --- Phase 6: publish ------------------------------------------------------

def publish(con, final_db):
    """Copy all nine objects into lookup_data.sqlite, in ONE transaction.

    A partial failure would otherwise leave new cq_zones beside a stale
    cq_zones_bbox - which does not error, it returns wrong geometry. R*Tree DDL
    is transactional, so the ROLLBACK undoes the virtual tables too.
    """
    con.isolation_level = None          # the only transaction is the one below
    con.execute("ATTACH DATABASE ? AS lookup", (final_db,))
    try:
        existed = con.execute(
            "SELECT COUNT(*) FROM lookup.sqlite_master "
            "WHERE type='table' AND name IN (?,?,?)", TABLES).fetchone()[0]
        log(f"{'Replacing' if existed else 'Creating'} {', '.join(TABLES)} in "
            f"{os.path.basename(final_db)} ...")

        con.execute("BEGIN IMMEDIATE")
        out = {}
        for table in TABLES:
            for stmt in ddl_for(table):
                con.execute(stmt.format(q="lookup."))
            con.execute(f"INSERT INTO lookup.{table} SELECT * FROM main.{table}")
            con.execute(f"INSERT INTO lookup.{table}_parts SELECT part_id, "
                        f"feature_id, geom FROM main.{table}_parts")
            con.execute(f"INSERT INTO lookup.{table}_bbox SELECT id, minx, "
                        f"maxx, miny, maxy, feature_id FROM main.{table}_bbox")
            con.execute(POST_INDEX.format(q="lookup.", t=table))
            out[table] = con.execute(
                f"SELECT COUNT(*) FROM lookup.{table}").fetchone()[0]
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("DETACH DATABASE lookup")
        raise
    con.execute("DETACH DATABASE lookup")
    for table, n in out.items():
        log(f"  {table}: {n:,} rows published")
    log(f"{'Replaced' if existed else 'Created'} the zone tables in "
        f"{final_db}{' (previous version discarded)' if existed else ''}")


# --- Main ------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="importer_zones.py",
        description="Build the `cq_zones`, `itu_zones` and `dxcc_entities` "
                    "tables of lookup_data.sqlite from the upstream GeoJSON "
                    "sources, downloading each one only if it has changed.")
    p.add_argument("--no-download", action="store_true",
                   help="do not touch the network: build from the GeoJSON "
                        "already in downloads/, and fail if it is not there.")
    return p


def run(args=None):
    """Run the whole import. run_importers.py calls this with no arguments;
    raises SystemExit on failure, which the menu reports."""
    global _log_fh, _repaired, _wrapped

    if args is None:
        args = build_parser().parse_args([])
    preflight()

    # Module state, reset because the menu may call run() twice in one process.
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None
    _notices.clear()
    _repaired = _wrapped = 0

    for d in (DOWNLOADS_DIR, CACHES_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)

    _log_fh = open(os.path.join(LOGS_DIR, RUN_LOG), "w", encoding="utf-8")
    t0 = time.time()
    log(f"=== Zone import started ({time.strftime('%Y-%m-%d')}) ===")
    log(f"Building into {WORK_DB}")
    log(f"  -> becomes the {', '.join(TABLES)} tables of {DB_PATH} on success")

    log("--- Phase 1: cleanup ---")
    cleanup_old_data()

    log("--- Phase 2: resolve + download sources ---")
    state = load_state()
    before = json.dumps(state, sort_keys=True)
    paths = {t: fetch_source(SOURCES[t], args.no_download, state) for t in TABLES}
    # Written once, after all three, so one source's failure cannot strand the
    # others' ETags: a run that dies mid-phase re-asks rather than 304s against
    # a file it never confirmed.
    if json.dumps(state, sort_keys=True) != before:
        save_state(state)
    for table in TABLES:
        log(f"  {SOURCES[table]['label']:<14} {paths[table][1]}")

    # try/finally, not a bare close(): run_importers.py returns to its menu on
    # failure, and on Windows a leaked handle makes the NEXT run's Phase 1
    # cleanup of WORK_DB fail outright ("being used by another process").
    con = sqlite3.connect(WORK_DB)
    try:
        con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        ok = True

        for phase, (table, loader) in enumerate(
                ((CQ_TABLE, lambda d: load_zones(d, "cq")),
                 (ITU_TABLE, lambda d: load_zones(d, "itu")),
                 (DXCC_TABLE, load_dxcc)), start=3):
            spec = SOURCES[table]
            log(f"--- Phase {phase}: build {table} ({spec['label']}) ---")
            _repaired = _wrapped = 0
            with open(paths[table][0], encoding="utf-8") as fh:
                doc = json.load(fh)
            n_feat, n_part = build_table(con, table, loader(doc))
            log(f"  {n_feat} feature(s), {n_part:,} part(s) from "
                f"{paths[table][1]}")
            if _repaired:
                log(f"  {_repaired} geometry(ies) repaired by make_valid")
            if _wrapped:
                log(f"  {_wrapped} feature(s) split at the antimeridian")
            ok &= verify_table(con, table, spec["features"])
            if table == DXCC_TABLE:
                ok &= verify_dxcc(con)
                report_overlaps(con, table, "prefix")
            else:
                ok &= verify_zone_numbers(con, table, spec["features"])
                report_overlaps(con, table, "zone")

        if not ok:
            sys.exit(f"ERROR: build verification FAILED - aborting before "
                     f"publish. The failed build is left at {WORK_DB} for "
                     f"inspection and is NOT published; {UNTOUCHED}")

        log("--- Phase 6: publish ---")
        publish(con, DB_PATH)
    finally:
        con.close()

    log(f"=== SUCCESS: {', '.join(TABLES)} in {DB_PATH} in "
        f"{time.time() - t0:,.1f} seconds ===")
    replay_notices()
    _log_fh.close()
    _log_fh = None


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user (Ctrl-C). The published tables are untouched - "
            "the work database is cleaned up by the next run.")
        try:
            if _log_fh:
                _log_fh.flush()
                _log_fh.close()
        except Exception:
            pass
        sys.exit(130)
