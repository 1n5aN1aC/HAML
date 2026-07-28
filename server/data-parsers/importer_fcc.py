#!/usr/bin/env python3
r"""
importer_fcc.py - FCC amateur license importer for lookup_data.sqlite.

Downloads, builds, geocodes and tags the FCC amateur dump, then publishes it as
the `fcc_operators` table of the shared lookup_data.sqlite. Driven by
run_importers.py (which calls run() directly) or runnable on its own.

  1 CLEANUP    remove a failed run's wreckage; the published table, the zip
               and the geocode cache are left alone
  2 DOWNLOAD   fetch and verify l_amat.zip, falling back to the local copy
  3 BUILD      parse it into caches/fcc_work.sqlite, one row per ACTIVE
               license, EN+HD+AM merged on unique_system_identifier, verified
               against the FCC `counts` manifest
  4 GEOCODE    distinct (street, city, state, zip5) via the US Census batch
               geocoder, through a persistent cache
  5 FALLBACK   unmatched rows get their ZIP's ZCTA centroid, or the nearest
               same-3-digit-prefix ZCTA
  6 COUNTY     point-in-polygon against Census county boundaries, restricted
               to counties of the licensee's own state
  7 DXCC       ARRL DXCC entity from the state code
  8 CONTINENT  'NA' / 'OC' from dxcc_id
  9 SECTION    ARRL Section from state, plus county in the 8 split states
               (CA, FL, MA, NJ, NY, PA, TX, WA)
 10 PUBLISH    write the unmatched-address report, then copy the finished
               table into lookup_data.sqlite in ONE transaction; until it
               commits, the published table is the previous run's

Phases 3-10 operate on the work database, never on lookup_data.sqlite. They are
steps in one pipeline, not independently runnable migrations.

Columns beyond the FCC's own: coordinates ("lat,lon" WGS-84, 6 decimals),
gridsquare (4-char Maidenhead), geocode_match (Exact / Non_Exact /
Zip_Centroid / Zip_Approx / NULL), county (short Census NAME, always in the
row's own state), dxcc_entity, dxcc_id, continent, arrl_section.

Usage
-----
    .venv\Scripts\python run_importers.py    # the menu; FCC is option 2
    .venv\Scripts\python importer_fcc.py     # this importer alone, with flags

    --miss-retry-days D   re-query a cached miss older than D days (default 30)
    --no-county           skip Phase 6, and with it shapely/pyshp and the
                          arrl_section of the 8 split states
    --no-ref-check        use the newest Census reference vintage already on
                          disk instead of probing for a newer one (offline)

Every path is fixed under the directory holding this script: lookup_data.sqlite,
downloads/ (l_amat.zip and the Census reference files), caches/ (work database
and geocode cache), logs/.

Each run adopts the newest reference vintage the Census publishes, raising a
banner when that changes. County NAME feeds both `county` and the Phase 9
section lookup, so a renamed or resplit county changes those columns between
runs with no code change here. Pin a vintage with min_vintage == max_vintage.

Exit status: 0 = success, non-zero = verification failure or a permanent
download/geocode failure (safe to rerun; the cache preserves progress).

Requires Python 3.9+ and `requests`; Phase 6 also needs `shapely` and `pyshp`.
Run through the project virtualenv:
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
"""

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests

import sections

# ===== Constants =========================================================== #

FCC_URL = "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BENCHMARK = "Public_AR_Current"
MAX_RETRIES = 6

# Prefixed: caches/ and logs/ are shared with the other importers.
CACHE_DB = "fcc_geocode_cache.sqlite"
RUN_LOG = "fcc_run.log"

# The one table this importer owns in lookup_data.sqlite.
TABLE = "fcc_operators"

# A named agent avoids the blanket bot rules that .gov endpoints apply to
# requests' default "python-requests/2.x" user agent.
HTTP_HEADERS = {"User-Agent": "fcc-amateur-db/1.0 (+bulk data refresh script)"}

# Addresses per Census upload, service's cap is 10000.
BATCH_SIZE = 2000

# Concurrent Census uploads. The service is a shared public endpoint, so this
# stays low enough to be a polite neighbour.
WORKERS = 5

# How long the main thread may block while waiting for batches. It has to be
# BOUNDED: see the comment in geocode_todo() - an unbounded wait cannot be
# interrupted on Windows, which is the whole reason this constant exists.
POLL_SECONDS = 0.5

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(HERE, "downloads")
CACHES_DIR = os.path.join(HERE, "caches")
LOGS_DIR = os.path.join(HERE, "logs")

# The shared database; this importer touches only TABLE inside it.
DB_PATH = os.path.join(HERE, "lookup_data.sqlite")

# Phases 3-9 build here; Phase 10 copies the finished table into DB_PATH.
WORK_DB = os.path.join(CACHES_DIR, "fcc_work.sqlite")

# Downloaded via ZIP_PATH + ".part" and renamed, so this name never points at a
# partial file. Never deleted: Phase 2 falls back to it when the FCC is down.
ZIP_PATH = os.path.join(DOWNLOADS_DIR, "l_amat.zip")

UNMATCHED_CSV = os.path.join(LOGS_DIR, "fcc_unmatched_addresses.csv")

# ---- state codes ---------------------------------------------------------- #

# The 48 contiguous states + DC are the single DXCC entity "United States of
# America". The set itself lives in sections.py: it is the same list that
# decides which states are one section, and the two must not drift.
CONTIGUOUS_STATES = sections.CONTIGUOUS_STATES

# state code -> (ARRL DXCC entity name, ARRL DXCC entity number).
DXCC_BY_STATE = {
    "AK": ("Alaska", 6),
    "HI": ("Hawaii", 110),
    "PR": ("Puerto Rico", 202),
    "VI": ("US Virgin Islands", 285),
    "GU": ("Guam", 103),
    "MP": ("Northern Mariana Islands", 166),
    "AS": ("American Samoa", 9),
}
# APO/FPO military mail: the station could be physically anywhere, so neither a
# DXCC entity nor an ARRL section applies.
MILITARY_STATES = sections.MILITARY_STATES

# Every code the FCC dump can legitimately carry, for case normalization.
USPS_STATES = (CONTIGUOUS_STATES | set(DXCC_BY_STATE) | MILITARY_STATES
               | {"UM"})

# ---- Census reference files (Phases 5 and 6) ------------------------------ #
#
# `min_vintage` is only the floor the upward probe starts from - the oldest
# layout this script handles - not the vintage in use. Set max_vintage equal to
# it to pin a vintage.
GAZETTEER_KEY = "zcta_gazetteer"
COUNTY_KEY = "county_500k"
REFERENCES = {
    GAZETTEER_KEY: {
        "min_vintage": 2025,
        "max_vintage": None,     # None = no ceiling; take the newest published
        "filename_template": "{y}_Gaz_zcta_national.zip",
        "url_template": (
            "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
            "{y}_Gazetteer/{y}_Gaz_zcta_national.zip"
        ),
        "desc": "ZCTA gazetteer (ZIP centroids)",
        "impact": [
            "Adopting it moves every Zip_Centroid and Zip_Approx",
            "coordinate in this run's output.",
        ],
    },
    COUNTY_KEY: {
        "min_vintage": 2025,
        "max_vintage": None,
        "shared": True,
        "filename_template": "cb_{y}_us_county_500k.zip",
        "url_template": (
            "https://www2.census.gov/geo/tiger/GENZ{y}/shp/"
            "cb_{y}_us_county_500k.zip"
        ),
        "desc": "county boundaries (1:500k)",
        "impact": [
            "County NAME feeds both the `county` and `arrl_section`",
            "columns, so a renamed or resplit county changes those",
            "columns' values in this run's output.",
        ],
    },
}

# ===== Logging (console + utf-8 log file) ================================== #

_print_lock = threading.Lock()
_log_fh = None

# Set by the Ctrl-C handler. Worker threads poll it so a stopped run winds down
# in seconds instead of grinding through every batch's retry schedule.
_INTERRUPTED = threading.Event()

BANNER_RULE = "-" * 70

# Banners worth seeing after a long run has scrolled by; re-emitted at the end.
_notices = []


def log(msg):
    # Blank spacer lines print bare - a timestamp on an empty line is noise.
    line = f"[{time.strftime('%H:%M:%S')}] {msg}" if msg else ""
    with _print_lock:
        print(line, flush=True)
        if _log_fh:
            _log_fh.write(line + "\n")
            _log_fh.flush()


def log_banner(lines):
    """Log a rule-delimited block that stands out in a wall of progress lines."""
    log("")
    for line in [BANNER_RULE, *lines, BANNER_RULE]:
        log(line)
    log("")
    _notices.append(lines)


def replay_notices():
    """Re-emit every banner raised during the run, so none are lost to scroll."""
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


# ===== Database connections ================================================ #
# Each phase opens and closes its own connection; these two let run() close
# whatever a phase that RAISED never got to. On Windows an orphaned handle makes
# the next run's Phase 1 cleanup of the work database fail outright, so the
# stale file gets reused rather than rebuilt.

_open_cons = []


def connect(path):
    """sqlite3.connect(), with the connection registered for leak cleanup."""
    con = sqlite3.connect(path)
    _open_cons.append(con)
    return con


def close_leaked_connections():
    """Close connections a raising phase left open; return how many there were."""
    leaked = 0
    while _open_cons:
        con = _open_cons.pop()
        try:
            con.execute("SELECT 1")     # ProgrammingError if already closed
        except Exception:
            continue
        try:
            con.close()
            leaked += 1
        except Exception:
            pass
    return leaked


# ===== FCC ULS record layouts ============================================== #
# Public Access Database Definitions; index = position in the pipe-split record.

AM_FIELDS = [
    "record_type", "unique_system_identifier", "uls_file_number", "ebf_number",
    "callsign", "operator_class", "group_code", "region_code",
    "trustee_callsign", "trustee_indicator", "physician_certification",
    "ve_signature", "systematic_call_sign_change", "vanity_call_sign_change",
    "vanity_relationship", "previous_callsign", "previous_operator_class",
    "trustee_name",
]  # 18

EN_FIELDS = [
    "record_type", "unique_system_identifier", "uls_file_number", "ebf_number",
    "callsign", "entity_type", "licensee_id", "entity_name", "first_name",
    "middle_initial", "last_name", "name_suffix", "phone", "fax", "email",
    "street_address", "city", "state", "zip_code", "po_box", "attention_line",
    "sgin", "frn", "applicant_type_code", "applicant_type_other",
    "status_code", "status_date", "lic_category_code", "linked_license_id",
    "linked_callsign",
]  # 30

HD_FIELDS = [
    "record_type", "unique_system_identifier", "uls_file_number", "ebf_number",
    "callsign", "license_status", "radio_service_code", "grant_date",
    "expired_date", "cancellation_date", "eligibility_rule_num",
    "applicant_type_code_reserved", "alien", "alien_government",
    "alien_corporation", "alien_officer", "alien_control", "revoked",
    "convicted", "adjudged", "involved_reserved", "common_carrier",
    "non_common_carrier", "private_comm", "fixed", "mobile", "radiolocation",
    "satellite", "developmental_or_sta", "interconnected_service",
    "certifier_first_name", "certifier_mi", "certifier_last_name",
    "certifier_suffix", "certifier_title", "gender", "african_american",
    "native_american", "hawaiian", "asian", "white", "ethnicity",
    "effective_date", "last_action_date", "auction_id",
    "reg_stat_broad_serv", "band_manager", "type_serv_broad_serv",
    "alien_ruling", "licensee_name_change", "whitespace_ind",
    "additional_cert_choice", "additional_cert_answer", "discontinuation_ind",
    "regulatory_compliance_ind", "eligibility_cert_900",
    "transition_plan_cert_900", "return_spectrum_cert_900", "payment_cert_900",
]  # 59

# Code -> description decodes (FCC ULS documentation).
OPERATOR_CLASS = {
    "A": "Advanced", "E": "Amateur Extra", "G": "General", "N": "Novice",
    "P": "Technician Plus", "T": "Technician",
}
APPLICANT_TYPE = {
    "B": "Amateur Club", "C": "Corporation", "D": "General Partnership",
    "E": "Limited Partnership", "F": "Limited Liability Partnership",
    "G": "Government Entity", "H": "Other", "I": "Individual",
    "J": "Joint Venture", "L": "Limited Liability Company",
    "M": "Military Recreation", "O": "Consortium", "P": "Partnership",
    "R": "RACES", "T": "Trust", "U": "Unincorporated Association",
}
RADIO_SERVICE = {"HA": "Amateur", "HV": "Amateur Vanity"}

DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def iso_date(v):
    """MM/DD/YYYY -> YYYY-MM-DD; anything else passes through (or None)."""
    if not v:
        return None
    m = DATE_RE.match(v)
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else v


# ===== Phase 1 - cleanup =================================================== #

def cleanup_old_data():
    """Delete a failed run's wreckage; the caches, zip and published table stay.

    The published table is replaced in one transaction only once its replacement
    is complete (Phase 10), and the zip by an atomic rename only once it is
    verified (Phase 2), so a run that dies before then leaves both intact.
    """
    victims = [
        WORK_DB,                         # half-built db from a failed run
        WORK_DB + "-journal",            # its rollback journal, if any
        ZIP_PATH + ".part",              # interrupted download
        UNMATCHED_CSV,
    ]
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
    log(f"Cleanup: {removed} stale file(s) removed; caches preserved. "
        f"The published {TABLE} table and the downloaded zip stay in place "
        f"until each is replaced atomically.")


# ===== Phase 2 - download ================================================== #

def usable_fcc_zip(path):
    """True if `path` is a readable zip carrying the FCC `counts` manifest."""
    if not os.path.exists(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return "counts" in zf.namelist()
    except Exception:
        return False


def download_fcc_zip(dest):
    """Stream l_amat.zip from the FCC with retries; atomic rename on success.

    Lands in <dest>.part and is proved openable before replacing the previous
    copy. If every attempt fails the run continues (loudly) on that copy, and
    exits only when there is no usable zip at all.
    """
    tmp = dest + ".part"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"Downloading {FCC_URL} (attempt {attempt}) ...")
            with requests.get(FCC_URL, stream=True, timeout=(30, 600),
                              headers=HTTP_HEADERS) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                next_pct = 10
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if total and done * 100 // total >= next_pct:
                            log(f"  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB "
                                f"({done * 100 // total}%)")
                            next_pct = done * 100 // total + 10
            # A connection cut mid-body just ends iter_content without raising.
            if total and done != total:
                raise RuntimeError(f"truncated: {done:,} of {total:,} bytes")
            with zipfile.ZipFile(tmp) as zf:
                if "counts" not in zf.namelist():
                    raise RuntimeError("zip has no `counts` manifest")
            os.replace(tmp, dest)
            log(f"Downloaded {os.path.getsize(dest) / 1e6:,.0f} MB -> {dest}")
            return
        except Exception as e:
            if attempt == MAX_RETRIES:
                log(f"  download failed ({e}); no attempts left")
                break
            wait = min(15 * attempt, 60)
            log(f"  download failed ({e}); retrying in {wait}s")
            time.sleep(wait)

    try:
        os.remove(tmp)                 # never leave a partial file behind
    except OSError:
        pass

    if usable_fcc_zip(dest):
        age_days = (time.time() - os.path.getmtime(dest)) / 86400.0
        log_banner([
            " NOTE: the FCC download failed - falling back to the local zip",
            "",
            f"   {FCC_URL}",
            f"   was unreachable after {MAX_RETRIES} attempts.",
            "",
            f"   Rebuilding from the existing {os.path.basename(dest)}"
            f" ({os.path.getsize(dest) / 1e6:,.0f} MB,",
            f"   downloaded {age_days:.0f} day(s) ago).",
            "",
            "   THE RESULTING DATABASE IS ONLY AS CURRENT AS THAT FILE.",
            "   Rerun once the FCC is reachable again.",
        ])
        return

    sys.exit(f"ERROR: could not download {FCC_URL} after {MAX_RETRIES} "
             f"attempts, and no usable local copy exists at {dest}")


# ===== Phase 3 - build the work database from the zip ====================== #

# One schema, two names: Phases 3-9 work on `operators` in the work database,
# Phase 10 creates the same thing as `fcc_operators` in lookup_data.sqlite.
# {q} is the schema qualifier: "" for the work database, "lookup." for the
# attached shared one. Each of these is ONE statement, executed individually:
# executescript() would COMMIT any open transaction first, and Phase 10 needs
# its drop/create/copy/index to be a single unit.
DROP_TABLE = "DROP TABLE IF EXISTS {q}{table}"

SCHEMA = """
CREATE TABLE {q}{table} (
    -- key ---------------------------------------------------------------
    unique_system_identifier INTEGER PRIMARY KEY,  -- FCC ULS internal license id
    callsign                 TEXT,
    -- licensee identity (EN.dat) ------------------------------------------
    entity_name              TEXT,   -- full name or club/org name
    first_name               TEXT,
    middle_initial           TEXT,
    last_name                TEXT,
    name_suffix              TEXT,
    street_address           TEXT,
    city                     TEXT,
    state                    TEXT,
    zip_code                 TEXT,
    po_box                   TEXT,
    attention_line           TEXT,
    frn                      TEXT,   -- FCC Registration Number
    applicant_type_code      TEXT,   -- I/B/G/M/R
    applicant_type           TEXT,   -- decoded
    -- license dates (HD.dat; every row is an Active license) ----------------
    radio_service_code       TEXT,   -- HA = Amateur, HV = Amateur Vanity
    radio_service            TEXT,   -- decoded
    grant_date               TEXT,   -- ISO YYYY-MM-DD
    expired_date             TEXT,
    convicted                TEXT,   -- felony-question answer on post-2017 apps
    -- amateur-specific (AM.dat; NULL for the ~2k licenses with no AM row) ----
    operator_class           TEXT,   -- A/E/G/N/P/T
    operator_class_desc      TEXT,   -- decoded
    group_code               TEXT,   -- callsign group A-D
    region_code              TEXT,   -- callsign region 0-10
    trustee_callsign         TEXT,   -- for club licenses
    trustee_indicator        TEXT,
    vanity_call_sign_change  TEXT,
    previous_callsign        TEXT,
    previous_operator_class  TEXT,
    trustee_name             TEXT,
    -- geocoding (filled in Phases 4-5) --------------------------------------
    coordinates              TEXT,   -- "lat,lon" WGS-84, 6 decimals
    gridsquare               TEXT,   -- 4-char Maidenhead locator
    geocode_match            TEXT,   -- Exact/Non_Exact/Zip_Centroid/Zip_Approx
    county                   TEXT,   -- short county-equivalent name (Phase 6)
    dxcc_entity              TEXT,   -- ARRL DXCC entity name (Phase 7)
    dxcc_id                  INTEGER, -- ARRL DXCC entity number (291=US, 110=HI)
    continent                TEXT,   -- 'NA' or 'OC', from dxcc_id (Phase 8)
    arrl_section             TEXT    -- ARRL Section abbreviation (Phase 9)
);
"""

INDEXES = (
    "CREATE UNIQUE INDEX {q}idx_{table}_callsign ON {table}(callsign)",
    # (state, county) takes Phase 9 from ~83 s of full table scans to ~3 s, and
    # is the natural index for the "hams per county in <state>" queries this
    # database exists to answer.
    "CREATE INDEX {q}idx_{table}_state_county ON {table}(state, county)",
    # Covering indexes for "every geocoded operator in <section>/<state>": the
    # coordinates column rides along so the lookup never touches the table.
    "CREATE INDEX {q}idx_{table}_section_coords"
    " ON {table}(arrl_section, coordinates)",
    "CREATE INDEX {q}idx_{table}_state_coords ON {table}(state, coordinates)",
)

# Only the first two earn their keep during the build. The coordinates indexes
# would be created before Phases 4-9 have written a single coordinate, and then
# maintained through every one of those bulk UPDATEs for no read benefit, so
# they are built once at publish time over finished data.
BUILD_INDEXES = INDEXES[:2]

# The name Phases 3-9 use inside the work database.
WORK_TABLE = "operators"


def read_records(zf, name, tag, n_fields, stats=None):
    """Yield cleaned field-lists from one .dat member of the zip.

    Physical lines that don't start with `tag|` are stitched onto the previous
    record (embedded newlines in free text). `stats['newlines']` receives the
    raw '\\n' count, which is what the FCC `counts` manifest (a `wc -l`) means.
    """
    prefix = tag + "|"
    buf = None
    newlines = 0
    with zf.open(name) as raw:
        fh = io.TextIOWrapper(raw, encoding="latin-1", newline="")
        for line in fh:
            newlines += line.count("\n")
            if line.startswith(prefix):
                if buf is not None:
                    yield _finish(buf, tag, n_fields)
                buf = line
            elif buf is not None:
                buf += line          # continuation of a multi-line record
        if buf is not None:
            yield _finish(buf, tag, n_fields)
    if stats is not None:
        stats["newlines"] = newlines


def _finish(record, tag, n_fields):
    parts = record.rstrip("\r\n").split("|")
    if len(parts) > n_fields:
        raise ValueError(f"{tag}: {len(parts)} fields, expected {n_fields}: "
                         f"{parts[:6]}")
    if len(parts) < n_fields:
        parts += [""] * (n_fields - len(parts))
    return [p.strip() or None for p in parts]


def expected_counts(zf):
    """Parse the `counts` member -> {'AM': 1688402, ...}."""
    out = {}
    with zf.open("counts") as fh:
        for line in io.TextIOWrapper(fh, encoding="latin-1"):
            m = re.match(r"\s*(\d+)\s+.*/(\w+)\.dat\s*$", line)
            if m:
                out[m.group(2)] = int(m.group(1))
    return out


def _bulk(con, sql, rows, size=50000):
    """Run `sql` over `rows` in batches; return how many rows were executed."""
    batch, n = [], 0
    for row in rows:
        batch.append(row)
        n += 1
        if len(batch) >= size:
            con.executemany(sql, batch)
            batch = []
    con.executemany(sql, batch)
    return n


def build_database(zip_path, db_path):
    """Parse l_amat.zip into the work database; abort on any count mismatch."""
    t0 = time.time()
    zf = zipfile.ZipFile(zip_path)
    expect = expected_counts(zf)
    log(f"Building {os.path.basename(db_path)} from {os.path.basename(zip_path)}")
    log(f"Expected record counts (FCC `counts` manifest): {expect}")

    if os.path.exists(db_path):
        os.remove(db_path)
    con = connect(db_path)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    con.execute(DROP_TABLE.format(q="", table=WORK_TABLE))
    con.execute(SCHEMA.format(q="", table=WORK_TABLE))

    loaded = {}    # records parsed per file (all statuses)
    kept = {}      # records stored per file (active licenses only)
    newlines = {}  # raw \n per file; the FCC `counts` manifest is a raw wc -l

    def records(datname, tag, fields):
        st = {}
        n = 0
        for r in read_records(zf, datname, tag, len(fields), stats=st):
            n += 1
            yield r
        newlines[tag] = st["newlines"]
        loaded[tag] = n

    # ---- pass 0: find the active licenses (HD license_status == 'A') ----
    log("Scanning HD.dat for active licenses ...")
    hi = {n_: i for i, n_ in enumerate(HD_FIELDS)}
    active = set()
    for r in read_records(zf, "HD.dat", "HD", len(HD_FIELDS)):
        if r[hi["license_status"]] == "A":
            active.add(int(r[hi["unique_system_identifier"]]))
    log(f"  {len(active):,} active licenses; all others are dropped")

    # ---- pass 1: EN (drives the operators table; one row per license) ----
    log("Loading EN.dat (entities) ...")
    en_cols = ["unique_system_identifier", "callsign", "entity_name",
               "first_name", "middle_initial", "last_name", "name_suffix",
               "street_address", "city", "state", "zip_code", "po_box",
               "attention_line", "frn", "applicant_type_code",
               "applicant_type"]
    ei = {n: i for i, n in enumerate(EN_FIELDS)}
    ins = (f"INSERT INTO operators ({','.join(en_cols)}) "
           f"VALUES ({','.join('?' * len(en_cols))})")
    kept["EN"] = _bulk(con, ins, (
        (usi, r[ei["callsign"]], r[ei["entity_name"]],
         r[ei["first_name"]], r[ei["middle_initial"]], r[ei["last_name"]],
         r[ei["name_suffix"]], r[ei["street_address"]], r[ei["city"]],
         r[ei["state"]], r[ei["zip_code"]], r[ei["po_box"]],
         r[ei["attention_line"]], r[ei["frn"]], r[ei["applicant_type_code"]],
         APPLICANT_TYPE.get(r[ei["applicant_type_code"]] or ""))
        for r in records("EN.dat", "EN", EN_FIELDS)
        if (usi := int(r[ei["unique_system_identifier"]])) in active
    ))

    # ---- pass 2: HD (license header -> UPDATE by primary key) ----
    # certifier_* fields are read but NOT stored (near-duplicates of the name).
    log("Loading HD.dat (license headers) ...")
    kept["HD"] = _bulk(con, """UPDATE operators SET radio_service_code=?,
             radio_service=?, grant_date=?, expired_date=?, convicted=?
             WHERE unique_system_identifier=?""", (
        (r[hi["radio_service_code"]],
         RADIO_SERVICE.get(r[hi["radio_service_code"]] or ""),
         iso_date(r[hi["grant_date"]]), iso_date(r[hi["expired_date"]]),
         r[hi["convicted"]], usi)
        for r in records("HD.dat", "HD", HD_FIELDS)
        if (usi := int(r[hi["unique_system_identifier"]])) in active
    ))

    # ---- pass 3: AM (amateur data -> UPDATE by primary key) ----
    log("Loading AM.dat (amateur data) ...")
    ai = {n_: i for i, n_ in enumerate(AM_FIELDS)}
    kept["AM"] = _bulk(con, """UPDATE operators SET operator_class=?,
             operator_class_desc=?, group_code=?, region_code=?,
             trustee_callsign=?, trustee_indicator=?, vanity_call_sign_change=?,
             previous_callsign=?, previous_operator_class=?, trustee_name=?
             WHERE unique_system_identifier=?""", (
        (r[ai["operator_class"]],
         OPERATOR_CLASS.get(r[ai["operator_class"]] or ""),
         r[ai["group_code"]], r[ai["region_code"]],
         r[ai["trustee_callsign"]], r[ai["trustee_indicator"]],
         r[ai["vanity_call_sign_change"]], r[ai["previous_callsign"]],
         r[ai["previous_operator_class"]], r[ai["trustee_name"]], usi)
        for r in records("AM.dat", "AM", AM_FIELDS)
        if (usi := int(r[ai["unique_system_identifier"]])) in active
    ))

    # ---- normalize stray mixed-case state codes ("Fl" -> "FL") ----
    # Only values that ARE a US state/territory code are touched; genuinely
    # foreign or blank states are left alone. Phase 4 is unaffected: the
    # geocode cache keys already uppercase state.
    variants = con.execute(
        "SELECT DISTINCT state FROM operators "
        "WHERE state IS NOT NULL AND state <> UPPER(state)"
    ).fetchall()
    fixes = [(s[0].upper(), s[0]) for s in variants if s[0].upper() in USPS_STATES]
    before = con.total_changes
    con.executemany("UPDATE operators SET state=? WHERE state=?", fixes)
    log(f"Normalized {con.total_changes - before} row(s) across "
        f"{len(fixes)} mixed-case state code(s)")

    # ---- duplicate callsigns: checked BEFORE the UNIQUE index below ----
    # Nothing in the dump guarantees one ACTIVE license per callsign. CREATE
    # UNIQUE INDEX would abort on that with a bare IntegrityError naming no
    # callsign - and with journal_mode=OFF there is no journal to roll the
    # half-built index back with.
    ok = True
    dups = con.execute(
        """SELECT callsign, COUNT(*) FROM operators WHERE callsign IS NOT NULL
           GROUP BY callsign HAVING COUNT(*) > 1 ORDER BY 2 DESC, 1"""
    ).fetchall()
    if dups:
        ok = False
        shown = ", ".join(f"{cs} x{n}" for cs, n in dups[:20])
        log(f"  DUPLICATE callsign(s) across active licenses: {len(dups)}"
            f" - {shown}{' ...' if len(dups) > 20 else ''}")

    if ok:
        log("Creating indexes ...")
        for stmt in BUILD_INDEXES:
            con.execute(stmt.format(q="", table=WORK_TABLE))
    else:
        log("Skipping index creation: the duplicates above would violate the "
            "UNIQUE index on callsign")
    con.commit()

    # ---- verification: the manifest is a raw line count, so compare it to
    # ---- the raw newlines consumed rather than to the logical record count
    log("--- Build verification ---")
    for tag, n in loaded.items():
        exp = expect.get(tag)
        raw = newlines.get(tag)
        status = "OK" if exp == raw else "MISMATCH"
        if exp != raw:
            ok = False
        note = "" if n == raw else f" ({raw - n} embedded newline(s) stitched)"
        log(f"  {tag}: raw lines {raw:>9,}  expected {exp:>9,}  "
            f"parsed {n:>9,}  kept (active) {kept[tag]:>9,}  {status}{note}")
    n_ops = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    n_am = con.execute(
        "SELECT COUNT(*) FROM operators WHERE operator_class IS NOT NULL"
    ).fetchone()[0]
    log(f"  operators rows: {n_ops:,} of {len(active):,} active licenses "
        f"(with operator_class: {n_am:,}; duplicated callsigns: {len(dups)})")
    if n_ops != len(active) or kept["HD"] != len(active):
        ok = False
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    zf.close()

    if not ok:
        sys.exit(f"ERROR: build verification FAILED for {db_path} - aborting "
                 f"before geocoding. The failed build is left on disk for "
                 f"inspection and is NOT promoted; any previous database is "
                 f"untouched.")
    log(f"Build OK: {os.path.getsize(db_path) / 1e6:,.0f} MB "
        f"in {time.time() - t0:,.0f}s")


# ===== Phase 4 - geocode (US Census batch geocoder + address cache) ======== #

def norm(s):
    """Normalize an address component for use as a dictionary key."""
    return (s or "").strip().upper()


def addr_key(street, city, state, zipc):
    return (norm(street), norm(city), norm(state), norm(zipc)[:5])


def maidenhead4(lat, lon):
    """4-character Maidenhead locator from decimal-degree lat/lon."""
    lon += 180.0
    lat += 90.0
    if not (0 <= lon < 360 and 0 <= lat < 180):
        return None
    return (
        chr(ord("A") + int(lon // 20))
        + chr(ord("A") + int(lat // 10))
        + str(int((lon % 20) // 2))
        + str(int((lat % 10) // 1))
    )


def extract_distinct_addresses(db_path):
    """Distinct geocodable address tuples, in addr_key() form.

    Keying the diff, the results and the final join identically is what lets a
    row with a NULL city/state/zip cache-hit at all.
    """
    con = connect(db_path)
    rows = con.execute("""
        SELECT DISTINCT
               UPPER(TRIM(street_address)),
               UPPER(TRIM(city)),
               UPPER(TRIM(state)),
               SUBSTR(TRIM(zip_code), 1, 5)
        FROM operators
        WHERE street_address IS NOT NULL AND TRIM(street_address) != ''
        ORDER BY 1, 2, 3, 4
    """).fetchall()
    con.close()
    seen, out = set(), []
    for r in rows:
        k = addr_key(*r)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def open_cache():
    """Open (creating if needed) the persistent content-addressed cache."""
    con = connect(os.path.join(CACHES_DIR, CACHE_DB))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            street     TEXT NOT NULL,
            city       TEXT NOT NULL,
            state      TEXT NOT NULL,
            zip5       TEXT NOT NULL,
            lat        REAL,
            lon        REAL,
            quality    TEXT,
            matched    INTEGER NOT NULL,   -- 1 = hit, 0 = miss
            fetched_at REAL NOT NULL,      -- unix seconds
            PRIMARY KEY (street, city, state, zip5)
        )
        """
    )
    con.commit()
    return con


def load_cache(con):
    """Return {addr_key: (matched, lat, lon, quality, fetched_at)}."""
    out = {}
    for street, city, state, zip5, lat, lon, quality, matched, fetched_at in (
        con.execute("SELECT street, city, state, zip5, lat, lon, quality, "
                    "matched, fetched_at FROM geocode_cache")
    ):
        out[(street, city, state, zip5)] = (matched, lat, lon, quality, fetched_at)
    return out


def upsert_cache(con, rows):
    """rows: (street, city, state, zip5, lat, lon, quality, matched, fetched_at)."""
    con.executemany(
        """
        INSERT INTO geocode_cache
            (street, city, state, zip5, lat, lon, quality, matched, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(street, city, state, zip5) DO UPDATE SET
            lat=excluded.lat, lon=excluded.lon, quality=excluded.quality,
            matched=excluded.matched, fetched_at=excluded.fetched_at
        """,
        rows,
    )
    con.commit()


def write_batch_csv(path, batch, start_idx):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for i, (street, city, state, zipc) in enumerate(batch):
            w.writerow([start_idx + i, street, city, state, zipc])


def submit_batch(batch_csv_path, result_path):
    """POST one batch file to the Census geocoder, save the result CSV."""
    for attempt in range(1, MAX_RETRIES + 1):
        if _INTERRUPTED.is_set():
            return False
        try:
            with open(batch_csv_path, "rb") as f:
                resp = requests.post(
                    CENSUS_URL,
                    files={"addressFile": ("addresses.csv", f, "text/csv")},
                    data={"benchmark": BENCHMARK},
                    timeout=(30, 1800),
                    headers=HTTP_HEADERS,
                )
            if resp.status_code == 200 and resp.content.strip():
                tmp = result_path + ".tmp"
                with open(tmp, "wb") as out:
                    out.write(resp.content)
                os.replace(tmp, result_path)
                return True
            raise RuntimeError(f"HTTP {resp.status_code}, {len(resp.content)} bytes")
        except Exception as e:
            name = os.path.basename(batch_csv_path)
            if attempt == MAX_RETRIES:
                log(f"  {name} attempt {attempt} failed ({e}); no attempts left")
                break
            wait = min(60 * attempt, 300)
            log(f"  {name} attempt {attempt} failed ({e}); retrying in {wait}s")
            # Interruptible: a Ctrl-C should not have to wait out a 300s backoff.
            if _INTERRUPTED.wait(wait):
                return False
    return False


def parse_result_csv(path):
    """{int_id: (lat, lon, quality, matched)} for every row, hit or miss.

    Census row: id, input_address, Match|No_Match|Tie[, Exact|Non_Exact,
    matched_address, "lon,lat", tigerline_id, side] - LONGITUDE-FIRST.
    """
    out = {}
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            try:
                rid = int(row[0])
            except ValueError:
                continue
            if row[2] == "Match" and len(row) >= 6 and row[5]:
                try:
                    lon_s, lat_s = row[5].split(",")
                    out[rid] = (float(lat_s), float(lon_s), row[3], 1)
                    continue
                except (ValueError, IndexError):
                    pass
            out[rid] = (None, None, None, 0)  # No_Match / Tie / malformed
    return out


def geocode_todo(con_cache, todo, now):
    """Geocode the queued addresses, writing each parsed batch into the cache."""
    work_dir = os.path.join(CACHES_DIR, "fcc_batch_work")
    os.makedirs(work_dir, exist_ok=True)
    # The cache is the only resume state, so anything left here by a crashed
    # run is dead weight.
    for f in os.listdir(work_dir):
        try:
            os.remove(os.path.join(work_dir, f))
        except OSError:
            pass

    batches = []  # (bno, start, csv_path, result_path)
    for bno, start in enumerate(range(0, len(todo), BATCH_SIZE)):
        chunk = todo[start:start + BATCH_SIZE]
        csv_path = os.path.join(work_dir, f"batch_{bno:04d}.csv")
        res_path = os.path.join(work_dir, f"batch_{bno:04d}.result.csv")
        write_batch_csv(csv_path, chunk, start)
        batches.append((bno, start, csv_path, res_path))

    log(f"{len(batches)} batch(es) to geocode ({len(todo)} addresses)")
    failed = []
    done = 0
    # Not a `with` block: its __exit__ joins every worker, and a worker parked
    # in a 30-minute Census read would hold a Ctrl-C hostage until that socket
    # timed out. Shutdown is driven explicitly below instead.
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futs = {
            pool.submit(submit_batch, cp, rp): (bno, cp, rp)
            for bno, start, cp, rp in batches
        }
        # Bounded waits, NOT as_completed(): a Ctrl-C only becomes a
        # KeyboardInterrupt when the MAIN thread reaches a bytecode boundary,
        # and an unbounded as_completed() gives it none until every batch has
        # resolved. On Windows that is fatal to the handler below - the wait is
        # a WaitForSingleObject that never looks at the pending signal, so a
        # Ctrl-C sits queued behind the entire geocode (workers can be parked
        # in a 1800s Census read). Returning every POLL_SECONDS is what makes
        # the interrupt land while there is still something to cancel.
        unfinished = set(futs)
        while unfinished:
            just_done, unfinished = wait(unfinished, timeout=POLL_SECONDS,
                                         return_when=FIRST_COMPLETED)
            for fut in just_done:
                bno, cp, rp = futs[fut]
                if not fut.result():
                    failed.append(bno)
                    log(f"batch {bno:04d} FAILED after {MAX_RETRIES} retries")
                    continue
                rows = [
                    (todo[idx][0], todo[idx][1], todo[idx][2], todo[idx][3],
                     lat, lon, quality, matched, now)
                    for idx, (lat, lon, quality, matched) in parse_result_csv(rp).items()
                ]
                upsert_cache(con_cache, rows)  # commit per batch = resume point
                done += 1
                log(f"batch {bno:04d} done ({done}/{len(batches)}), {len(rows)} cached")
                for p in (cp, rp):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    except KeyboardInterrupt:
        # Every completed batch is already committed to the cache.
        _INTERRUPTED.set()
        log(f"  Ctrl-C: stopping geocode ({done}/{len(batches)} batch(es) done "
            f"this run, all cached) ...")
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    try:
        os.rmdir(work_dir)
    except OSError:
        pass  # leftover temp files from a failed batch; harmless
    return failed


def geocode_database(db, miss_retry_days):
    """Phase 4 driver: cache diff -> Census batches -> UPDATE operators."""
    log("Extracting distinct addresses...")
    addresses = extract_distinct_addresses(db)
    log(f"{len(addresses)} distinct geocodable addresses")

    con_cache = open_cache()
    cache = load_cache(con_cache)
    now = time.time()
    retry_before = now - miss_retry_days * 86400.0

    todo = []
    hits = fresh_miss = 0
    for a in addresses:
        ent = cache.get(a)
        if ent is None:
            todo.append(a)                      # new / changed address
        elif ent[0]:
            hits += 1                           # cached match -> reuse
        elif ent[4] < retry_before:
            todo.append(a)                      # stale miss -> retry
        else:
            fresh_miss += 1                     # recent miss -> skip for now
    log(f"cache: {hits} matched reused, {fresh_miss} recent misses skipped, "
        f"{len(todo)} to geocode ({len(cache)} entries total)")

    if todo:
        failed = geocode_todo(con_cache, todo, now)
        if failed:
            con_cache.close()
            sys.exit(f"ERROR: batches failed permanently: {sorted(failed)} - "
                     f"rerun to retry (their addresses stay uncached)")
    else:
        log("Nothing new to geocode; using cache as-is.")

    cache = load_cache(con_cache)
    con_cache.close()
    geo = {k: (v[1], v[2], v[3]) for k, v in cache.items() if v[0]}
    matched = sum(1 for a in addresses if a in geo)
    log(f"{matched}/{len(addresses)} distinct addresses matched "
        f"({matched / max(len(addresses), 1):.1%})")

    log("Updating database with street-level coordinates + gridsquares...")
    con = connect(db)
    updates = []
    rows_matched = 0
    cur = con.execute("SELECT unique_system_identifier, street_address, city, "
                      "state, zip_code FROM operators")
    for pk, street, city, state, zipc in cur:
        hit = geo.get(addr_key(street, city, state, zipc))
        if hit:
            lat, lon, qual = hit
            updates.append((f"{lat:.6f},{lon:.6f}", maidenhead4(lat, lon),
                            qual, pk))
            rows_matched += 1
    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?",
        updates,
    )
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    con.close()
    log(f"{rows_matched}/{total} license rows have street-level coordinates.")


# ===== Census reference files ============================================== #
# Probe upward from min_vintage for the newest vintage published; use the local
# copy if it is already that vintage, otherwise download it, delete the
# superseded one, and raise a banner. If the probe or download fails, fall back
# to the newest local vintage (loudly). --no-ref-check skips the probe.

def _ref_url(spec, vintage):
    return spec["url_template"].format(y=vintage)


def _ref_path(spec, vintage):
    return os.path.join(DOWNLOADS_DIR,
                        spec["filename_template"].format(y=vintage))


def _local_vintages(spec):
    """Vintages of this reference file present in downloads/, newest first."""
    pat = re.escape(spec["filename_template"]).replace(r"\{y\}", r"(\d{4})")
    out = [int(m.group(1)) for m in
           (re.fullmatch(pat, name) for name in os.listdir(DOWNLOADS_DIR)) if m]
    return sorted(out, reverse=True)


def _download_ref(spec, vintage, path):
    """Fetch one vintage. Verifies it is a readable zip before it lands."""
    url = _ref_url(spec, vintage)
    log(f"Downloading {spec['desc']} vintage {vintage} from {url}")
    resp = requests.get(url, timeout=(30, 300), headers=HTTP_HEADERS)
    resp.raise_for_status()
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(resp.content)
    # A truncated 200 would otherwise sit there looking like a good file until
    # Phase 5 or 6 opened it.
    try:
        with zipfile.ZipFile(tmp) as zf:
            if zf.testzip() is not None:
                raise RuntimeError("corrupt member")
    except Exception:
        os.remove(tmp)
        raise
    os.replace(tmp, path)
    log(f"{spec['desc']}: {len(resp.content):,} bytes -> {os.path.basename(path)}")


def _newest_published(spec):
    """Highest vintage the Census actually serves, or None if none answered.

    HEAD probes from min_vintage up to max_vintage, or to next year when unset
    (a vintage appears before its nominal year is out). Raises only if EVERY
    probe raised.
    """
    floor = spec["min_vintage"]
    ceiling = spec["max_vintage"] or (time.gmtime().tm_year + 1)
    newest = None
    errors = tried = 0
    for y in range(floor, ceiling + 1):
        tried += 1
        try:
            r = requests.head(_ref_url(spec, y), timeout=(10, 30),
                              allow_redirects=True, headers=HTTP_HEADERS)
        except requests.RequestException as e:
            errors += 1
            last = e
            continue
        if r.status_code == 200:
            newest = y
    if newest is None and tried and errors == tried:
        raise last
    return newest


def ensure_reference(key, ref_opts):
    """Local path to the newest available vintage of a reference file.

    A change of vintage raises a banner, because it moves `county` and
    `arrl_section` values.
    """
    spec = REFERENCES[key]
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    local = _local_vintages(spec)        # newest first

    want = None
    if ref_opts["check"]:
        try:
            want = _newest_published(spec)
        except requests.RequestException as e:
            log(f"WARNING: {spec['desc']} vintage probe failed ({e})")
    else:
        log(f"{spec['desc']}: vintage probe skipped (--no-ref-check)")

    if want is None:
        if not local:
            sys.exit(f"ERROR: cannot determine a {spec['desc']} vintage to use "
                     f"and none is present in {DOWNLOADS_DIR}"
                     + (" (--no-ref-check, so nothing was probed)"
                        if not ref_opts["check"] else ""))
        path = _ref_path(spec, local[0])
        log(f"{spec['desc']}: using local vintage {local[0]} "
            f"({os.path.basename(path)})")
        return path

    path = _ref_path(spec, want)
    if os.path.exists(path):
        log(f"{spec['desc']}: vintage {want} is current ({os.path.basename(path)})")
        return path

    try:
        _download_ref(spec, want, path)
    except Exception as e:
        # Fall back to any other copy rather than losing Phase 5/6 entirely.
        older = [v for v in local if v != want]
        if not older:
            sys.exit(f"ERROR: cannot obtain {spec['desc']} vintage {want} "
                     f"({e}) and no other copy exists in {DOWNLOADS_DIR}")
        path = _ref_path(spec, older[0])
        log_banner([
            f" NOTE: falling back to an older {spec['desc']}",
            "",
            f"   vintage {want} could not be downloaded:",
            f"     {e}",
            "",
            f"   Using vintage {older[0]} already on disk instead.",
            "   Results will reflect that older vintage.",
        ])
        return path

    if not spec.get("shared"):
        for old in local:
            if old != want:
                try:
                    os.remove(_ref_path(spec, old))
                    log(f"  removed superseded vintage {old}")
                except OSError:
                    pass

    if local:
        log_banner([
            f" NOTE: the {spec['desc']} vintage CHANGED",
            "",
            f"   previously cached:  vintage {local[0]}",
            f"   this run uses:      vintage {want}",
            "",
            *[f"   {line}" for line in spec["impact"]],
            "",
            "   If Phase 9 reports county names it cannot map, this is why:",
            "   add them to SPLIT_SECTIONS in sections.py (which importer_ca.py",
            "   and importer_boundaries.py read too, so one edit covers all).",
            "",
            f"   To stay on {local[0]} instead, set both min_vintage and",
            f"   max_vintage to {local[0]} in REFERENCES[{key!r}].",
        ])
    else:
        log(f"{spec['desc']}: vintage {want} (first download)")
    return path


# ===== Phase 5 - ZIP-centroid fallback (ZCTA Gazetteer) ==================== #

def load_zip_centroids(ref_opts):
    """{zip5: (lat, lon)} from the Census ZCTA Gazetteer."""
    zip_path = ensure_reference(GAZETTEER_KEY, ref_opts)
    centroids = {}
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".txt"))
        with io.TextIOWrapper(zf.open(name), encoding="utf-8-sig") as f:
            first = f.readline()
            delim = "|" if "|" in first else "\t"
            header = [h.strip() for h in first.split(delim)]
            i_geoid = header.index("GEOID")
            i_lat = header.index("INTPTLAT")
            i_lon = header.index("INTPTLONG")
            for line in f:
                parts = line.split(delim)
                try:
                    centroids[parts[i_geoid].strip()] = (
                        float(parts[i_lat]),
                        float(parts[i_lon]),
                    )
                except (ValueError, IndexError):
                    continue
    log(f"{len(centroids)} ZIP (ZCTA) centroids loaded")
    return centroids


def apply_zip_fallback(db, ref_opts):
    """Fill coordinates from ZIP centroids for rows the street geocode missed."""
    centroids = load_zip_centroids(ref_opts)
    con = connect(db)
    cur = con.execute(
        "SELECT unique_system_identifier, zip_code FROM operators "
        "WHERE coordinates IS NULL AND zip_code IS NOT NULL"
    )
    # ZCTAs grouped by 3-digit prefix for the nearest-ZIP approximation:
    # PO-Box-only and "unique" ZIPs have no ZCTA of their own, but are almost
    # always numerically adjacent to their town's street ZIP.
    by_prefix = {}
    for z in centroids:
        if z.isdigit():
            by_prefix.setdefault(z[:3], []).append(int(z))

    def nearest_zcta(zip5):
        if not zip5.isdigit():
            return None
        cands = by_prefix.get(zip5[:3])
        if not cands:
            return None
        return f"{min(cands, key=lambda c: abs(c - int(zip5))):05d}"

    updates = []
    exact_n = approx_n = 0
    for pk, zipc in cur:
        zip5 = norm(zipc)[:5]
        hit = centroids.get(zip5)
        if hit:
            quality = "Zip_Centroid"
            exact_n += 1
        else:
            near = nearest_zcta(zip5)
            if not near:
                continue
            hit = centroids[near]
            quality = "Zip_Approx"
            approx_n += 1
        lat, lon = hit
        updates.append(
            (f"{lat:.6f},{lon:.6f}", maidenhead4(lat, lon), quality, pk)
        )
    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?",
        updates,
    )
    con.commit()
    remaining = con.execute(
        "SELECT COUNT(*) FROM operators WHERE coordinates IS NULL"
    ).fetchone()[0]
    con.close()
    log(f"ZIP fallback: {exact_n} rows from own-ZIP centroid, "
        f"{approx_n} from nearest same-prefix ZCTA (Zip_Approx); "
        f"{remaining} rows remain without coordinates.")


# ===== Phase 6 - county (point-in-polygon vs Census boundaries) ============ #

# How far (degrees, ~111 km each) a point outside every polygon of its own state
# may be snapped to the nearest county in it. Beyond this the coordinate itself
# is wrong rather than merely offshore, and a snapped county would read exactly
# like a real lookup. The two populations barely overlap: of 241 outside points,
# 121 sit within 0.25 deg, 6 in 0.25-0.5, and 114 beyond 0.5.
FAR_SNAP_DEGREES = 0.5


def load_county_shapes(ref_opts):
    """(geometries, names, states) from the Census county boundary file.

    `names` is the short NAME field ("Jefferson"), not NAMELSAD ("Jefferson
    Parish"), per the schema contract for `county`; `states` is STUSPS.
    """
    import shapefile as pyshp
    from shapely.geometry import shape as shapely_shape

    zip_path = ensure_reference(COUNTY_KEY, ref_opts)
    with zipfile.ZipFile(zip_path) as zf:
        # Member names come from the archive, so a Census change to the
        # internal naming does not need one here.
        shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
        base = shp_name[:-4]
        rdr = pyshp.Reader(
            shp=io.BytesIO(zf.read(base + ".shp")),
            shx=io.BytesIO(zf.read(base + ".shx")),
            dbf=io.BytesIO(zf.read(base + ".dbf")),
        )
        # A tripwire: failing here beats a KeyError 40 minutes into a run.
        if "STUSPS" not in [f[0] for f in rdr.fields]:
            sys.exit(f"ERROR: {shp_name} has no STUSPS field; Phase 6 needs it "
                     f"to confine county lookup to the licensee's state")
        geoms, names, states = [], [], []
        for sr in rdr.iterShapeRecords():
            geoms.append(shapely_shape(sr.shape.__geo_interface__))
            names.append(sr.record["NAME"])
            states.append(sr.record["STUSPS"])
    log(f"{len(geoms)} county(-equivalent) polygons loaded "
        f"across {len(set(states))} states/territories")
    return geoms, names, states


def assign_counties(db, ref_opts):
    """Fill `county` for every row with coordinates, within its own state.

    Distinct (coordinates, state) pairs are resolved once via a bulk STRtree
    containment query against the counties of that state ONLY - a geocode just
    across a state line would otherwise take the neighbour's county, which is
    wrong in `county` and unmappable in Phase 9. Points inside no polygon of
    their own state snap to the nearest county in it, within FAR_SNAP_DEGREES;
    states with no county polygons at all (UM, foreign codes) stay NULL.
    """
    import shapely
    from shapely.strtree import STRtree

    geoms, names, states = load_county_shapes(ref_opts)

    # One tree per state, so a point can only ever match a county of its own.
    by_state = {}
    for g, nm, st in zip(geoms, names, states):
        gs, ns = by_state.setdefault(st, ([], []))
        gs.append(g)
        ns.append(nm)
    trees = {st: (STRtree(gs), gs, ns) for st, (gs, ns) in by_state.items()}

    con = connect(db)
    pairs = con.execute(
        "SELECT DISTINCT coordinates, state FROM operators "
        "WHERE coordinates IS NOT NULL AND county IS NULL"
    ).fetchall()
    if not pairs:
        con.close()
        log("County: nothing to assign.")
        return
    log(f"Resolving county for {len(pairs)} distinct (coordinate, state) pair(s) ...")

    groups = {}
    for c, st in pairs:
        groups.setdefault(st, []).append(c)

    coord_county = {}
    snapped = no_polygons = done = logged = 0
    far = []
    for st, clist in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        entry = trees.get(st)
        if entry is None:
            # UM and any foreign/malformed code: no US county-equivalent
            # exists, so leave it NULL rather than snapping to the mainland.
            for c in clist:
                coord_county[(c, st)] = None
            no_polygons += len(clist)
            continue
        tree, sgeoms, snames = entry

        lats, lons = [], []
        for c in clist:
            lat_s, lon_s = c.split(",")
            lats.append(float(lat_s))
            lons.append(float(lon_s))
        pts = shapely.points(lons, lats)  # shapely is (x, y) = (lon, lat)

        county_of = [None] * len(clist)
        chunk = 50000
        for start in range(0, len(pts), chunk):
            pt_idx, poly_idx = tree.query(pts[start:start + chunk],
                                          predicate="within")
            for pi, gi in zip(pt_idx, poly_idx):
                county_of[start + pi] = snames[gi]  # border ties: last wins

        for i, name in enumerate(county_of):
            if name is not None:
                continue
            gi = tree.nearest(pts[i])
            dist = sgeoms[gi].distance(pts[i])
            if dist > FAR_SNAP_DEGREES:
                far.append((dist, st, snames[gi], clist[i]))
            else:
                county_of[i] = snames[gi]
                snapped += 1

        for c, name in zip(clist, county_of):
            coord_county[(c, st)] = name
        done += len(clist)
        if done - logged >= 100000:
            log(f"  point-in-polygon {done}/{len(pairs)}")
            logged = done

    log(f"  {snapped} point(s) outside every polygon of their own state "
        f"snapped to the nearest county in that state")
    if no_polygons:
        log(f"  {no_polygons} point(s) in states with no county polygons "
            f"left without a county")
    if far:
        log(f"  {len(far)} point(s) left WITHOUT a county: the nearest county "
            f"of their own state is more than {FAR_SNAP_DEGREES} deg "
            f"(~{FAR_SNAP_DEGREES * 111:.0f} km) away, so the coordinate is "
            f"wrong (usually a mistyped ZIP) and any county would be invented")
        for dist, st, name, c in sorted(far, reverse=True)[:10]:
            log(f"    {dist:6.1f} deg from {st}/{name:<20} at {c}")

    # Update by primary key: coordinates is unindexed, so updating by it would
    # full-scan the table per distinct coordinate.
    rows = con.execute(
        "SELECT unique_system_identifier, coordinates, state FROM operators "
        "WHERE coordinates IS NOT NULL AND county IS NULL"
    ).fetchall()
    con.executemany(
        "UPDATE operators SET county=? WHERE unique_system_identifier=?",
        [(coord_county[(c, st)], pk) for pk, c, st in rows
         if coord_county.get((c, st)) is not None],
    )
    con.commit()
    n = con.execute(
        "SELECT COUNT(*) FROM operators WHERE county IS NOT NULL"
    ).fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    con.close()
    log(f"County: {n:,}/{total:,} rows assigned "
        f"({snapped} point(s) snapped to the nearest county in their state).")


# ===== Phase 7 - DXCC entity =============================================== #
# DXCC entity follows the station's physical location, NOT its callsign (US
# callsigns are portable - a KH6 prefix can be held from Ohio; measured
# agreement is only ~83-93%), so the state code is the key.

def dxcc_for_state(state):
    """(entity_name, dxcc_id) for a state code; (None, None) if undeterminable."""
    st = (state or "").strip().upper()
    if st in CONTIGUOUS_STATES:
        return ("United States of America", 291)
    if st in DXCC_BY_STATE:
        return DXCC_BY_STATE[st]
    if st in MILITARY_STATES:
        return ("Military (APO/FPO)", None)
    return (None, None)


def assign_dxcc(db):
    """Fill dxcc_entity / dxcc_id from each row's (normalized) state code."""
    con = connect(db)
    # Only a few dozen distinct state strings; resolve each once, then bulk
    # UPDATE by exact stored value (`IS` is NULL-safe, so blank states match).
    states = [r[0] for r in con.execute("SELECT DISTINCT state FROM operators")]
    con.executemany(
        "UPDATE operators SET dxcc_entity=?, dxcc_id=? WHERE state IS ?",
        [(*dxcc_for_state(st), st) for st in states],
    )
    con.commit()

    log("DXCC entity breakdown:")
    for ent, n in con.execute(
        "SELECT COALESCE(dxcc_entity, '(undetermined)'), COUNT(*) "
        "FROM operators GROUP BY dxcc_entity ORDER BY COUNT(*) DESC"
    ):
        log(f"  {ent:>26}: {n:>9,}")
    con.close()


# ===== Phase 8 - continent (NA / OC lookup table) ========================== #
# Only two continents occur in the US amateur file. Military/undeterminable rows
# have no dxcc_id and so no row here, leaving their continent NULL.
CONTINENT_BY_DXCC_ID = {
    291: "NA",   # United States of America (48 contiguous + DC)
    6:   "NA",   # Alaska
    202: "NA",   # Puerto Rico
    285: "NA",   # US Virgin Islands
    110: "OC",   # Hawaii
    103: "OC",   # Guam
    166: "OC",   # Northern Mariana Islands
    9:   "OC",   # American Samoa
}


def assign_continent(db):
    """Fill `continent` from each row's dxcc_id. Requires Phase 7."""
    con = connect(db)
    con.executemany(
        "UPDATE operators SET continent=? WHERE dxcc_id=?",
        [(c, i) for i, c in CONTINENT_BY_DXCC_ID.items()],
    )
    con.commit()

    log("Continent breakdown:")
    for cont, n in con.execute(
        "SELECT COALESCE(continent, '(none)'), COUNT(*) FROM operators "
        "GROUP BY continent ORDER BY COUNT(*) DESC"
    ):
        log(f"  {cont:>6}: {n:>9,}")
    con.close()


# ===== Phase 9 - ARRL Section ============================================== #

# The 8 states split into multiple sections along county lines, the county
# names they are keyed on, and the 1:1 mapping for every other state, all live
# in sections.py - importer_ca.py and importer_boundaries.py key on the same
# names and must give the same answer. Phase 6 stores the Census NAME form
# those tables expect.
SPLIT_STATES = sections.SPLIT_STATES
SECTION_BY_COUNTY = sections.SECTION_BY_COUNTY


def assign_sections(db):
    """Fill `arrl_section` from state (and county for split states)."""
    con = connect(db)
    for st, county_map in SECTION_BY_COUNTY.items():
        db_counties = {r[0] for r in con.execute(
            "SELECT DISTINCT county FROM operators "
            "WHERE state=? AND county IS NOT NULL", (st,)
        )}
        # Cross-state geocode snaps (address in one state, coordinates in a
        # neighbour's county) are expected for a handful of rows; they stay
        # NULL and are not bugs in the table.
        unmapped = db_counties - set(county_map)
        if unmapped:
            log(f"  note: {st} has {len(unmapped)} cross-state county name(s) "
                f"(will be NULL): {sorted(unmapped)}")
        unused = set(county_map) - db_counties
        if unused:
            log(f"  note: {st} has {len(unused)} county name(s) in table "
                f"not seen in DB: {sorted(unused)}")

    states = [r[0] for r in con.execute("SELECT DISTINCT state FROM operators")]
    con.executemany(
        "UPDATE operators SET arrl_section=? WHERE state IS ?",
        [(sections.us_section(st, None), st) for st in states
         if (st or "").strip().upper() not in SPLIT_STATES],
    )
    for st in SPLIT_STATES:
        pairs = con.execute(
            "SELECT DISTINCT county FROM operators "
            "WHERE state=? AND county IS NOT NULL", (st,)
        ).fetchall()
        con.executemany(
            "UPDATE operators SET arrl_section=? WHERE state=? AND county IS ?",
            [(SECTION_BY_COUNTY[st].get(c), st, c) for (c,) in pairs],
        )
    con.commit()

    log("ARRL section breakdown:")
    for sec, n in con.execute(
        "SELECT COALESCE(arrl_section, '(none)'), COUNT(*) "
        "FROM operators GROUP BY arrl_section ORDER BY COUNT(*) DESC"
    ):
        log(f"  {sec:>6}: {n:>9,}")
    con.close()


# ===== Phase 10 - publish (report, summary, copy into the lookup DB) ======= #

def finalize(db, final_db):
    """Report, then publish the finished table into the shared database."""
    con = connect(db)

    rows = con.execute(
        "SELECT callsign, street_address, city, state, zip_code, po_box "
        "FROM operators WHERE coordinates IS NULL ORDER BY callsign"
    ).fetchall()
    with open(UNMATCHED_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["callsign", "street_address", "city", "state", "zip_code",
                    "po_box"])
        w.writerows(rows)
    log(f"{len(rows)} rows without coordinates -> {UNMATCHED_CSV}")

    log("--- Final summary ---")
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    located = 0
    for match, n in con.execute(
        "SELECT COALESCE(geocode_match, '(none)'), COUNT(*) FROM operators "
        "GROUP BY geocode_match ORDER BY COUNT(*) DESC"
    ):
        log(f"  {match:>14}: {n:>9,}")
        if match != "(none)":
            located += n
    log(f"  {'located':>14}: {located:>9,} / {total:,} ({located / total:.2%})")
    for label, col in (("with county", "county"),
                       ("with section", "arrl_section")):
        n = con.execute(
            f"SELECT COUNT(*) FROM operators WHERE {col} IS NOT NULL"
        ).fetchone()[0]
        log(f"  {label:>14}: {n:>9,} / {total:,} ({n / total:.2%})")

    # No VACUUM: publish() copies rows into a freshly created table, so the
    # published result is already compact, and the work database is deleted by
    # the next run anyway.
    publish(con, final_db)
    con.close()


def publish(con, final_db):
    """Copy the finished work table into lookup_data.sqlite as TABLE.

    Drop, create, copy and reindex happen inside ONE transaction on the attached
    database, so a crash partway through rolls back to the previously published
    table. Only TABLE is touched; other importers' tables are merely locked for
    the duration of the copy.
    """
    # Autocommit mode, so the only transaction is the explicit one below:
    # sqlite3's default opens transactions implicitly around DML, which would
    # collide with the BEGIN here (and ATTACH cannot run inside one).
    con.isolation_level = None
    con.execute("ATTACH DATABASE ? AS lookup", (final_db,))
    try:
        replacing = con.execute(
            "SELECT COUNT(*) FROM lookup.sqlite_master "
            "WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()[0] > 0
        log(f"{'Replacing' if replacing else 'Creating'} {TABLE} in "
            f"{os.path.basename(final_db)} ...")

        con.execute("BEGIN IMMEDIATE")
        con.execute(DROP_TABLE.format(q="lookup.", table=TABLE))
        con.execute(SCHEMA.format(q="lookup.", table=TABLE))
        con.execute(f"INSERT INTO lookup.{TABLE} "
                    f"SELECT * FROM main.{WORK_TABLE}")
        for stmt in INDEXES:
            con.execute(stmt.format(q="lookup.", table=TABLE))
        n = con.execute(f"SELECT COUNT(*) FROM lookup.{TABLE}").fetchone()[0]
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("DETACH DATABASE lookup")
        raise
    con.execute("DETACH DATABASE lookup")
    log(f"{'Replaced' if replacing else 'Created'} {TABLE} "
        f"({n:,} rows) in {final_db}"
        f"{' (previous version discarded)' if replacing else ''}")


# ===== Main ================================================================ #

def preflight(args):
    """Abort before Phase 1 if Phase 6's packages are missing.

    It imports them lazily, roughly an hour into a cold run; discovering the
    gap there costs the download, the build and the whole geocode.
    """
    if args.no_county:
        return
    missing = []
    for mod in ("shapely", "shapefile"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return
    # pip name != import name for pyshp, and the message has to name what you
    # would actually type.
    pkgs = sorted({"shapefile": "pyshp", "shapely": "shapely"}[m]
                  for m in missing)
    sys.exit(
        f"ERROR: Phase 6 (county) needs {', '.join(pkgs)}.\n"
        f"Install:  python -m pip install {' '.join(pkgs)}\n"
        "          (or: python -m pip install -r requirements.txt)\n"
        "Or skip it: python importer_fcc.py --no-county\n"
        "  - county and arrl_section stay NULL in the 8 split states."
    )


def build_parser():
    ap = argparse.ArgumentParser(
        prog="importer_fcc.py",
        description="Full FCC amateur import: cleanup, download, build, "
                    "geocode, gridsquares, then publish as the "
                    f"`{TABLE}` table of lookup_data.sqlite.")
    ap.add_argument("--miss-retry-days", type=float, default=30.0,
                    help="re-query a cached miss once it is older than this many "
                         "days (0 = always retry misses)")
    ap.add_argument("--no-county", action="store_true",
                    help="skip the county assignment phase")
    ap.add_argument("--no-ref-check", action="store_true",
                    help="do not probe for newer Census reference vintages; "
                         "use the newest one already on disk (offline runs)")
    return ap


def run(args=None):
    """Run the whole import. `args` is a parsed Namespace, or None for defaults.

    run_importers.py calls this with no arguments, which is exactly a flagless
    command-line run, and catches the SystemExit a failing phase raises.
    """
    global _log_fh

    if args is None:
        args = build_parser().parse_args([])

    # Module state is per-process and run_importers.py runs importers in ITS
    # process, possibly twice in one session. Reset what would carry over: a
    # log handle left open by a run that died, the previous run's banners, and
    # a set interrupt flag that would make every geocode batch return at once.
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None
    _notices.clear()
    _INTERRUPTED.clear()
    _open_cons.clear()

    preflight(args)
    ref_opts = {"check": not args.no_ref_check}

    for d in (DOWNLOADS_DIR, CACHES_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)

    _log_fh = open(os.path.join(LOGS_DIR, RUN_LOG), "w", encoding="utf-8")
    t0 = time.time()
    log(f"=== FCC amateur import started ({time.strftime('%Y-%m-%d')}) ===")
    log(f"Building into {WORK_DB}")
    log(f"  -> becomes the {TABLE} table of {DB_PATH} on success"
        f"{'' if os.path.exists(DB_PATH) else ' (no lookup database yet; created)'}")

    try:
        log("--- Phase 1: cleanup ---")
        cleanup_old_data()

        log("--- Phase 2: download ---")
        download_fcc_zip(ZIP_PATH)

        log("--- Phase 3: build database ---")
        build_database(ZIP_PATH, WORK_DB)

        log("--- Phase 4: geocode (Census batch + cache) ---")
        geocode_database(WORK_DB, args.miss_retry_days)

        log("--- Phase 5: ZIP-centroid fallback ---")
        apply_zip_fallback(WORK_DB, ref_opts)

        log("--- Phase 6: county assignment ---")
        if args.no_county:
            log("skipped (--no-county)")
        else:
            assign_counties(WORK_DB, ref_opts)

        log("--- Phase 7: DXCC entity ---")
        assign_dxcc(WORK_DB)

        log("--- Phase 8: continent (NA/OC lookup) ---")
        assign_continent(WORK_DB)

        log("--- Phase 9: ARRL section ---")
        assign_sections(WORK_DB)

        log("--- Phase 10: publish ---")
        finalize(WORK_DB, DB_PATH)
    finally:
        # Close whatever a phase that RAISED never got to close.
        leaked = close_leaked_connections()
        if leaked:
            log(f"  closed {leaked} database connection(s) left open by a "
                f"phase that failed")

    log(f"=== SUCCESS: {TABLE} in {DB_PATH} "
        f"in {(time.time() - t0) / 60:,.1f} minutes ===")
    replay_notices()
    _log_fh.close()
    _log_fh = None


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _INTERRUPTED.set()
        log("Interrupted by user (Ctrl-C). Any completed geocode batches are "
            f"cached; rerun the same command to resume. The published {TABLE} "
            "table is untouched - the work database is cleaned up by the next "
            "run.")
        try:
            if _log_fh:
                _log_fh.flush()
                _log_fh.close()
        except Exception:
            pass
        # A plain sys.exit() would hang: at interpreter shutdown the
        # ThreadPoolExecutor's atexit hook JOINS every worker thread, and one
        # blocked in a slow Census read won't return until its 1800s socket
        # timeout. Progress is already committed to the cache.
        os._exit(130)
