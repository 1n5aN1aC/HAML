#!/usr/bin/env python3
r"""
update_fcc_db.py - one-shot FCC amateur license database refresh pipeline.

Download, build, geocode, and tag the whole database in a single run:

  Phase 1  CLEANUP    - delete what a previous run may have STRANDED (a
                        half-built .new database, a partial .part download).
                        The live database and zip are
                        deliberately NOT deleted - each is replaced by an atomic
                        rename only once its successor is complete, so a run
                        that dies before then leaves the previous good copy
                        exactly where it was. The address-lookup cache
                        (geocode_cache/) is PRESERVED; update_run.log is not
                        deleted here but truncated when the run opens it.
  Phase 2  DOWNLOAD   - fetch a fresh l_amat.zip from
                        https://data.fcc.gov/download/pub/uls/complete/l_amat.zip
                        into l_amat.zip.part, verify it opens and carries the
                        `counts` manifest, and only then rename it over the
                        previous copy. If every attempt fails but the previous
                        copy is intact, the run continues on it (loudly) rather
                        than abandoning everything downstream.
  Phase 3  BUILD      - parse the zip into fcc_amateur.sqlite.new: one
                        `operators` table, one row per ACTIVE license, EN+HD+AM
                        merged on unique_system_identifier, FCC codes decoded,
                        dates ISO. Load is verified against the FCC's own
                        `counts` manifest and the run aborts on any mismatch.
  Phase 4  GEOCODE    - resolve every distinct (street, city, state, zip5) via
                        the US Census batch geocoder, through a persistent
                        content-addressed cache so reruns only pay for
                        new/changed addresses.
  Phase 5  FALLBACK   - rows the street geocoder could not match get the
                        interior-point centroid of their 5-digit ZIP (ZCTA
                        Gazetteer); ZIPs with no ZCTA fall back to the
                        numerically nearest same-3-digit-prefix ZCTA.
  Phase 6  COUNTY     - point-in-polygon lookup of every coordinate against
                        the Census cartographic county boundary file
                        (downloaded once into the cache dir), restricted to
                        counties of the licensee's own state so a geocode that
                        lands just across a state line cannot be credited to
                        the neighbour; points that fall just outside every
                        polygon of that state (offshore geocodes) snap to the
                        nearest county in it, while points too far out to be a
                        coastal miss are left NULL rather than given an
                        invented county. Stores the short name (no
                        "County"/"Parish"/"Borough" suffix).
  Phase 7  DXCC       - derive the ARRL DXCC entity from each licensee's state
                        code: the 48 contiguous states + DC are one entity
                        ("United States"), while Alaska, Hawaii, and the five
                        inhabited island territories are separate entities.
  Phase 8  CONTINENT  - fill the `continent` column ('NA' or 'OC') from each
                        row's dxcc_id: US/Alaska/Puerto Rico/US Virgin Islands
                        are North America, Hawaii and the Pacific territories
                        are Oceania. APO/FPO military and undeterminable rows
                        have no dxcc_id, so their continent stays NULL.
  Phase 9  SECTION    - derive the ARRL Section abbreviation from each
                        licensee's state code (and county for the 8 states
                        split into multiple sections: CA, FL, MA, NJ, NY, PA,
                        TX, WA). Depends on Phase 6 (county) for split states;
                        non-split states resolve from state alone. MD+DC merge
                        to MDC; HI and Pacific territories merge to PAC.
  Phase 10 FINALIZE   - VACUUM and print a coverage summary (including the
                        residue that never located); then rename the finished
                        .new database over the previous one. This is the moment
                        the old database is replaced - up to here it is intact
                        and queryable, including while the ~1-hour geocode runs.

Phases 3-10 all operate on fcc_amateur.sqlite.new, never on the live database.
Phase 3 deletes and recreates that working file on every run, so every column
below already exists by the time the later phases run and none of them alter
the schema. They are steps in one pipeline, not independently runnable
migrations: pointing them at a database built by an older version of this
script is not
supported.

Cleanup processes applied during the build:
  - only ACTIVE licenses kept; columns that are 100% empty in the amateur
    dump, constant-for-active columns, and ULS bookkeeping fields are never
    created.
  - state codes stored mixed-case in the dump ("Fl", "az") are normalized to
    canonical uppercase USPS form during the build; foreign/blank values are
    left untouched.
  - the five certifier_* name columns (near-duplicates of the licensee's own
    name) are excluded. They are never built in the first place rather than
    created and then dropped, for an identical end schema with no wasted I/O.

Geocoding columns added to `operators`:
  coordinates    TEXT  "lat,lon"  WGS-84 decimal degrees, 6 decimal places
  gridsquare     TEXT  4-character Maidenhead locator (e.g. "EN75")
  geocode_match  TEXT  'Exact' / 'Non_Exact'   street-level Census match
                       'Zip_Centroid'          own-ZIP ZCTA centroid
                       'Zip_Approx'            nearest same-prefix ZCTA
                       NULL                    not geocodable (APO/FPO etc.)
  county         TEXT  short county-equivalent name from the Census boundary
                       file NAME field (e.g. "Jefferson", "Anchorage"), always
                       a county of the row's own `state`; NULL where
                       coordinates is NULL, where the state has no US
                       county-equivalents at all (UM, foreign codes), or where
                       the coordinate is too far from any county of its own
                       state to be trusted (mistyped ZIPs)
  dxcc_entity    TEXT  ARRL DXCC entity name ("United States", "Hawaii",
                       "Puerto Rico", ...); "Military (APO/FPO)" for AA/AE/AP;
                       NULL for foreign/blank/undeterminable state codes
  dxcc_id        INT   ARRL DXCC entity number (291 US, 6 AK, 110 HI, 202 PR,
                       285 VI, 103 GU, 166 MP, 9 AS); NULL where no single
                       entity applies
  continent      TEXT  'NA' (North America) or 'OC' (Oceania), derived from
                       dxcc_id; NULL for military/undeterminable rows
  arrl_section   TEXT  ARRL Section abbreviation (e.g. "ENY", "STX", "MDC");
                       derived from state (and county for split states);
                       NULL for military/foreign/blank or un-geocoded rows
                       in the 8 split states (CA/FL/MA/NJ/NY/PA/TX/WA)

Usage
-----
    ..\.venv\Scripts\python update_fcc_db.py  # full refresh in script's folder

Every path this pipeline owns is fixed, beside the script:

    fcc_amateur.sqlite        the database (built as .new, renamed on success)
    l_amat.zip                the FCC dump (downloaded as .part, then renamed)
    update_run.log            this run's log
    geocode_cache/            the persistent cache and Census reference files

There is no flag to relocate the database or to supply your own zip. Both
existed to protect against a Phase 1 that deleted them before the download;
Phase 1 now removes only the wreckage of a failed run, and Phase 2 falls back
to the existing l_amat.zip on its own when the FCC is unreachable.

    --cache-dir PATH      geocode cache dir  (default: geocode_cache here)
    --workers N           concurrent Census uploads (default 3)
    --miss-retry-days D   re-query a cached miss older than D days (default 30;
                          0 = always retry misses)
    --no-zip-fallback     skip Phase 5
    --no-county           skip Phase 6 (county assignment)
    --no-dxcc             skip Phase 7 (DXCC entity; also skips Phase 8, which
                          derives continent from dxcc_id)
    --no-continent        skip Phase 8 (continent NA/OC lookup table)
    --no-section          skip Phase 9 (ARRL section assignment)
    --no-ref-check        never check the reference files (offline runs)

Census reference files
----------------------
The ZCTA gazetteer and county boundary file live in the cache dir named after
the vintage they came from (2025_Gaz_zcta_national.zip,
cb_2025_us_county_500k.zip). The vintage in use is PINNED in the REFERENCES
table near the top of this file, and the filename is derived from it, so the
pin and the file cannot disagree: either the pinned file is on disk or it is
downloaded.

  - the pinned file is present            -> use it
  - it is not                             -> download it, delete the superseded
                                             vintage
  - the download fails but an older
    vintage is in the cache               -> use that instead, with a banner
  - a newer vintage exists upstream       -> banner naming the line to edit

Adopting a newer vintage is therefore a one-number code edit and a rerun,
never automatic: county NAME feeds both `county` and the Phase 9 ARRL section
lookup, so a renamed or resplit county would silently change results. The
check is a couple of HEAD requests and runs every invocation, so a pending
upgrade keeps announcing itself; a failed check is logged and the run
continues. --no-ref-check skips it for fully offline runs.

Exit status: 0 = success, non-zero = verification failure / permanent
download or geocode failure (safe to rerun; the cache preserves progress).

Requires Python 3.9+ and `requests`; Phase 6 (county) additionally needs
`shapely` and `pyshp` for point-in-polygon (skip with --no-county to avoid
them, at the cost of `county` and the 8 split states' `arrl_section`).
Everything else is stdlib. Missing packages are reported before Phase 1 rather
than an hour into the run.

Run through the project virtualenv, not the system Python. It lives one level
up, in data-parsers/, and is shared with the Canadian pipeline:
..\.venv\Scripts\python update_fcc_db.py   (Windows) /
../.venv/bin/python update_fcc_db.py       (macOS/Linux)

One-time setup, from the data-parsers folder:
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt

Use `python -m pip` rather than `pip.exe`: a venv's console-script shims
hardcode an absolute path to the interpreter that created them, so they keep
targeting the original location if the folder is ever copied or renamed.
"""

import argparse
import csv
import hashlib
import io
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

FCC_URL = "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BENCHMARK = "Public_AR_Current"
CACHE_DB = "geocode_cache.sqlite"
MAX_RETRIES = 6
RUN_LOG = "update_run.log"

# Sent on every outbound request. requests' default ("python-requests/2.x")
# identifies nothing and is a common target for blanket bot rules on public
# .gov endpoints, which surface as a 403 on a URL that works in a browser.
# A named agent is both politer and less likely to be swept up in one.
HTTP_HEADERS = {"User-Agent": "fcc-amateur-db/1.0 (+bulk data refresh script)"}

# Addresses per Census batch upload. 9000 rather than the service's 10000 cap:
# sitting exactly at the limit leaves no headroom for any disagreement about
# what counts as a row, and a slightly smaller file is a shorter server-side
# job with less exposure to a gateway timeout. A full cold run is ~82 batches.
BATCH_SIZE = 9000

# Census reference files (Phases 5 and 6).
#
# ---- THE VINTAGE IN USE IS PINNED HERE. To adopt a newer one, change the ----
# ---- `vintage` number below and rerun; the run tells you when one exists. ----
#
# That number is the whole configuration. The local file is named after it
# (cb_2025_us_county_500k.zip), so the filename alone says which vintage is on
# disk - nothing else to keep in sync, and nothing that can get out of sync.
# Changing the number makes the pinned file "missing", which makes the next run
# download it and delete the superseded one.
#
# Neither file auto-updates. County NAME feeds both `county` and the Phase 9
# ARRL section lookup, so a renamed or resplit county would silently change
# results; the gazetteer follows the same rule for one less special case.
GAZETTEER_KEY = "zcta_gazetteer"
COUNTY_KEY = "county_500k"
REFERENCES = {
    GAZETTEER_KEY: {
        "vintage": 2025,
        "filename_template": "{y}_Gaz_zcta_national.zip",
        "url_template": (
            "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
            "{y}_Gazetteer/{y}_Gaz_zcta_national.zip"
        ),
        "desc": "ZCTA gazetteer (ZIP centroids)",
        "why_manual": [
            "Adopting it moves every Zip_Centroid and Zip_Approx",
            "coordinate, so take it when you want that, not mid-week.",
        ],
    },
    COUNTY_KEY: {
        "vintage": 2025,
        "filename_template": "cb_{y}_us_county_500k.zip",
        "url_template": (
            "https://www2.census.gov/geo/tiger/GENZ{y}/shp/"
            "cb_{y}_us_county_500k.zip"
        ),
        "desc": "county boundaries (1:500k)",
        "why_manual": [
            "County NAME feeds both the `county` and `arrl_section`",
            "columns, so a renamed or resplit county would silently",
            "change results.",
        ],
    },
}

HERE = os.path.dirname(os.path.abspath(__file__))

# Every file this pipeline owns lives beside the script, under a fixed name.
# These were once --db / --zip, but relocating them only ever created ways for
# the run's own artifacts to drift apart: the report and log stayed here while
# the database moved, so Phase 1 cleaned a path the previous run had not
# written. One directory, one set of names, nothing to keep in sync.
DB_PATH = os.path.join(HERE, "fcc_amateur.sqlite")

# Phases 3-10 build here; Phase 10 renames it onto DB_PATH once it is provably
# complete. Until that instant DB_PATH still holds the previous run's database.
WORK_DB = DB_PATH + ".new"

# The FCC dump. Downloaded via ZIP_PATH + ".part" and renamed into place only
# after it is proved openable, so this name never points at a partial file. It
# is deliberately never deleted: when the FCC is unreachable, Phase 2 falls
# back to it rather than abandoning the run.
ZIP_PATH = os.path.join(HERE, "l_amat.zip")

# --------------------------------------------------------------------------- #
# Logging (console + utf-8 log file)
# --------------------------------------------------------------------------- #

_print_lock = threading.Lock()
_log_fh = None

# Set by the Ctrl-C handler. Worker threads poll it so a stopped run winds down
# in seconds instead of grinding through every remaining batch's retry schedule
# (6 attempts backing off to 300s each - minutes per batch, all of it pointless
# once the user has asked to stop).
_INTERRUPTED = threading.Event()


def log(msg):
    # Blank spacer lines (banners) print bare - a timestamp on an empty line
    # is just noise, and leaves trailing whitespace in the log file.
    line = f"[{time.strftime('%H:%M:%S')}] {msg}" if msg else ""
    with _print_lock:
        print(line, flush=True)
        if _log_fh:
            _log_fh.write(line + "\n")
            _log_fh.flush()


BANNER_RULE = "-" * 70

# Banner notices worth seeing after a long run has scrolled by; re-emitted
# together at the very end of main().
_notices = []


def log_banner(lines, repeat_at_end=True):
    """Log a rule-delimited block that stands out in a wall of progress lines."""
    block = [BANNER_RULE, *lines, BANNER_RULE]
    log("")
    for line in block:
        log(line)
    log("")
    if repeat_at_end:
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


# --------------------------------------------------------------------------- #
# FCC ULS record layouts (Public Access Database Definitions, license DB).
# Index = 0-based position in the pipe-split record. Verified field-by-field
# against the dump.
# --------------------------------------------------------------------------- #

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

HS_FIELDS = ["record_type", "unique_system_identifier", "uls_file_number",
             "callsign", "log_date", "code"]  # 6

CO_FIELDS = ["record_type", "unique_system_identifier", "uls_file_number",
             "callsign", "comment_date", "description", "status_code",
             "status_date"]  # 8; `description` is free text (may hold | and newlines)

LA_FIELDS = ["record_type", "unique_system_identifier", "callsign",
             "attachment_code", "attachment_description", "attachment_date",
             "attachment_filename", "action_performed"]  # 8

SC_FIELDS = ["record_type", "unique_system_identifier", "uls_file_number",
             "ebf_number", "callsign", "special_condition_type",
             "special_condition_code", "status_code", "status_date"]  # 9

SF_FIELDS = ["record_type", "unique_system_identifier", "uls_file_number",
             "ebf_number", "callsign", "license_free_form_type",
             "unique_license_free_form_identifier", "sequence_number",
             "license_free_form_condition", "status_code", "status_date"]  # 11

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


# --------------------------------------------------------------------------- #
# Phase 1 - cleanup
# --------------------------------------------------------------------------- #

def cleanup_old_data():
    """Delete what a previous run stranded. geocode_cache/ is never touched.

    Deliberately does NOT delete the live database or zip. Each is replaced by
    an atomic rename only once its successor is fully built (Phase 10) or fully
    downloaded and verified (Phase 2), so a run that dies anywhere before then
    leaves the previous good copy in place - and Phase 2 can even fall back to
    the old zip when the FCC is unreachable. What belongs here is only the
    wreckage of a *failed* run: the half-built work database and a partial
    download.

    Unconditional. This once had a --skip-cleanup escape hatch, from when Phase
    1 deleted the database and zip outright and a failed run therefore cost you
    both; now that it removes only wreckage, opting out just means starting on
    top of the last crash's leftovers.
    """
    victims = [
        DB_PATH + ".original.bak",       # legacy backup, if present
        WORK_DB,                         # half-built db from a failed run
        WORK_DB + "-journal",            # its rollback journal, if any
        ZIP_PATH + ".part",              # interrupted download
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
    log(f"Cleanup: {removed} stale file(s) removed; geocode cache preserved. "
        f"The existing database and zip stay in place until replaced "
        f"atomically.")


# --------------------------------------------------------------------------- #
# Phase 2 - download
# --------------------------------------------------------------------------- #

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

    The download lands in <dest>.part and is proved openable (and to carry the
    `counts` manifest) BEFORE it is renamed over the previous copy, so a failed
    or truncated fetch can never destroy a good one.

    Returns True if a fresh copy was downloaded, False if every attempt failed
    but the previous copy is intact and the run is proceeding on it. Exits only
    when there is no usable zip at all - no later phase means anything without
    one.
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
            # A connection cut mid-body just ends iter_content without raising,
            # so compare against the advertised length before trusting the file.
            if total and done != total:
                raise RuntimeError(f"truncated: {done:,} of {total:,} bytes")
            # sanity check: must be a readable zip containing the manifest
            with zipfile.ZipFile(tmp) as zf:
                if "counts" not in zf.namelist():
                    raise RuntimeError("zip has no `counts` manifest")
            os.replace(tmp, dest)
            log(f"Downloaded {os.path.getsize(dest) / 1e6:,.0f} MB -> {dest}")
            return True
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

    # Phase 1 deliberately left the previous zip in place. If it is intact,
    # rebuilding from it beats aborting: every phase after this one still runs
    # to completion and still verifies - just against older FCC data.
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
        return False

    sys.exit(f"ERROR: could not download {FCC_URL} after {MAX_RETRIES} "
             f"attempts, and no usable local copy exists at {dest}")


# --------------------------------------------------------------------------- #
# Phase 3 - build the sqlite from the zip (certifier columns omitted at build
# time rather than dropped afterwards)
# --------------------------------------------------------------------------- #

SCHEMA = """
DROP TABLE IF EXISTS operators;
CREATE TABLE operators (
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

INDEXES = """
CREATE UNIQUE INDEX idx_operators_callsign ON operators(callsign);
-- (state, county) carries Phase 9: without it each of its ~660 per-state and
-- per-county UPDATEs scans the whole table (~83 s); with it the phase runs in
-- ~3 s. Costs ~3 s of index maintenance during the Phase 6 county writeback
-- and ~16 MB in the finished file, and it is the natural index for the
-- "hams per county in <state>" queries this database exists to answer.
CREATE INDEX idx_operators_state_county ON operators(state, county);
"""


def read_records(zf, name, tag, n_fields, text_field=None, stats=None):
    """Yield cleaned field-lists from one .dat member of the zip.

    Stitches physical lines that don't start with `tag|` onto the previous
    record (embedded newlines in free text, seen in CO.dat). If a record
    splits into more than n_fields (free text containing '|'), the middle
    fields are re-joined into `text_field`.

    If `stats` (a dict) is given, records stats['newlines'] = number of raw
    '\\n' characters consumed. The FCC `counts` manifest is a raw `wc -l`,
    so it equals this newline count — NOT the logical record count — for
    files whose free text contains embedded newlines.
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
                    yield _finish(buf, tag, n_fields, text_field)
                buf = line
            elif buf is not None:
                buf += line          # continuation of a multi-line record
            # else: stray leading junk (never observed) - ignored
        if buf is not None:
            yield _finish(buf, tag, n_fields, text_field)
    if stats is not None:
        stats["newlines"] = newlines


def _finish(record, tag, n_fields, text_field):
    parts = record.rstrip("\r\n").split("|")
    if len(parts) > n_fields:
        if text_field is None:
            raise ValueError(f"{tag}: {len(parts)} fields, expected {n_fields}: {parts[:6]}")
        # re-join overflow into the free-text field, preserving inner newlines
        head = parts[:text_field]
        tail = parts[len(parts) - (n_fields - text_field - 1):]
        middle = "|".join(parts[text_field:len(parts) - (n_fields - text_field - 1)])
        parts = head + [middle] + tail
    elif len(parts) < n_fields:
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


def build_database(zip_path, db_path):
    """Parse l_amat.zip into fcc_amateur.sqlite; abort on any count mismatch."""
    t0 = time.time()
    zf = zipfile.ZipFile(zip_path)
    expect = expected_counts(zf)
    log(f"Building {os.path.basename(db_path)} from {os.path.basename(zip_path)}")
    log(f"Expected record counts (FCC `counts` manifest): {expect}")

    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    con.executescript(SCHEMA)

    loaded = {}    # records parsed per file (all statuses)
    kept = {}      # records stored per file (active licenses only)
    newlines = {}  # raw \n per file; the FCC `counts` manifest is a raw wc -l

    def records(datname, tag, fields, text_field=None):
        st = {}
        n = 0
        for r in read_records(zf, datname, tag, len(fields), text_field, stats=st):
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
    batch, n = [], 0
    ins = f"INSERT INTO operators ({','.join(en_cols)}) VALUES ({','.join('?' * len(en_cols))})"
    for r in records("EN.dat", "EN", EN_FIELDS):
        usi = int(r[ei["unique_system_identifier"]])
        if usi not in active:
            continue
        batch.append((
            usi, r[ei["callsign"]], r[ei["entity_name"]],
            r[ei["first_name"]], r[ei["middle_initial"]], r[ei["last_name"]],
            r[ei["name_suffix"]], r[ei["street_address"]], r[ei["city"]],
            r[ei["state"]], r[ei["zip_code"]], r[ei["po_box"]],
            r[ei["attention_line"]], r[ei["frn"]],
            r[ei["applicant_type_code"]],
            APPLICANT_TYPE.get(r[ei["applicant_type_code"]] or ""),
        ))
        n += 1
        if len(batch) >= 50000:
            con.executemany(ins, batch); batch = []
    con.executemany(ins, batch)
    kept["EN"] = n

    # ---- pass 2: HD (license header -> UPDATE by primary key). ----
    # certifier_* fields are read but NOT stored (near-duplicates of the name).
    log("Loading HD.dat (license headers) ...")
    upd = """UPDATE operators SET radio_service_code=?, radio_service=?,
             grant_date=?, expired_date=?, convicted=?
             WHERE unique_system_identifier=?"""
    batch, n = [], 0
    for r in records("HD.dat", "HD", HD_FIELDS):
        usi = int(r[hi["unique_system_identifier"]])
        if usi not in active:
            continue
        batch.append((
            r[hi["radio_service_code"]],
            RADIO_SERVICE.get(r[hi["radio_service_code"]] or ""),
            iso_date(r[hi["grant_date"]]), iso_date(r[hi["expired_date"]]),
            r[hi["convicted"]],
            usi,
        ))
        n += 1
        if len(batch) >= 50000:
            con.executemany(upd, batch); batch = []
    con.executemany(upd, batch)
    kept["HD"] = n

    # ---- pass 3: AM (amateur data -> UPDATE by primary key) ----
    log("Loading AM.dat (amateur data) ...")
    ai = {n_: i for i, n_ in enumerate(AM_FIELDS)}
    upd = """UPDATE operators SET operator_class=?, operator_class_desc=?,
             group_code=?, region_code=?, trustee_callsign=?,
             trustee_indicator=?, vanity_call_sign_change=?,
             previous_callsign=?, previous_operator_class=?, trustee_name=?
             WHERE unique_system_identifier=?"""
    batch, n = [], 0
    for r in records("AM.dat", "AM", AM_FIELDS):
        usi = int(r[ai["unique_system_identifier"]])
        if usi not in active:
            continue
        batch.append((
            r[ai["operator_class"]],
            OPERATOR_CLASS.get(r[ai["operator_class"]] or ""),
            r[ai["group_code"]], r[ai["region_code"]],
            r[ai["trustee_callsign"]], r[ai["trustee_indicator"]],
            r[ai["vanity_call_sign_change"]], r[ai["previous_callsign"]],
            r[ai["previous_operator_class"]], r[ai["trustee_name"]],
            usi,
        ))
        n += 1
        if len(batch) >= 50000:
            con.executemany(upd, batch); batch = []
    con.executemany(upd, batch)
    kept["AM"] = n

    # ---- normalize stray mixed-case state codes ("Fl" -> "FL") ----
    # The dump carries ~300 rows with lower/title-case state codes. Fold them
    # to canonical uppercase USPS form so per-state grouping is clean. Only
    # values that ARE a US state/territory code (case-insensitively) are
    # touched; genuinely foreign or blank states are left alone. Geocode cache
    # keys already uppercase state, so this does not affect Phase 4.
    usps = frozenset({
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
        "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
        "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
        "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
        "WV", "WI", "WY", "PR", "VI", "GU", "MP", "AS", "AA", "AE", "AP", "UM",
    })
    variants = con.execute(
        "SELECT DISTINCT state FROM operators "
        "WHERE state IS NOT NULL AND state <> UPPER(state)"
    ).fetchall()
    fixes = [(s[0].upper(), s[0]) for s in variants if s[0].upper() in usps]
    before = con.total_changes
    con.executemany("UPDATE operators SET state=? WHERE state=?", fixes)
    log(f"Normalized {con.total_changes - before} row(s) across "
        f"{len(fixes)} mixed-case state code(s)")

    # ---- supplementary files: not stored, but still parsed so their ----
    # ---- integrity is verified against the FCC `counts` manifest    ----
    for datname, tag, fields, text_field in [
        ("HS.dat", "HS", HS_FIELDS, None),
        ("CO.dat", "CO", CO_FIELDS, CO_FIELDS.index("description")),
        ("LA.dat", "LA", LA_FIELDS, None),
        ("SC.dat", "SC", SC_FIELDS, None),
        ("SF.dat", "SF", SF_FIELDS, SF_FIELDS.index("license_free_form_condition")),
    ]:
        log(f"Verifying {datname} (not stored) ...")
        for _ in records(datname, tag, fields, text_field):
            pass

    # ---- duplicate callsigns: checked BEFORE the UNIQUE index below ----
    # Nothing in the dump guarantees one ACTIVE license per callsign (a vanity
    # grant caught mid-transition would produce two). CREATE UNIQUE INDEX would
    # abort on that with a bare IntegrityError naming no callsign - and with
    # journal_mode=OFF there is no journal to roll the half-built index back
    # with. Catching it here routes the failure through the normal verification
    # report instead, and names the offenders.
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
        con.executescript(INDEXES)
    else:
        log("Skipping index creation: the duplicates above would violate the "
            "UNIQUE index on callsign")
    con.commit()

    # ---- verification ----
    # The FCC `counts` manifest is a raw line (`wc -l`) count, so it is
    # compared against the raw newlines consumed. Logical records can be
    # fewer when free text contains embedded newlines (CO.dat).
    log("--- Build verification ---")
    # NB: `ok` is seeded by the duplicate-callsign check above - do not reset it
    for tag, n in loaded.items():
        exp = expect.get(tag)
        raw = newlines.get(tag)
        status = "OK" if exp == raw else "MISMATCH"
        if exp != raw:
            ok = False
        note = "" if n == raw else f" ({raw - n} embedded newline(s) stitched)"
        keptcol = f"kept (active) {kept[tag]:>9,}" if tag in kept else "not stored          "
        log(f"  {tag}: raw lines {raw:>9,}  expected {exp:>9,}  "
            f"parsed {n:>9,}  {keptcol}  {status}{note}")
    n_ops = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    n_am = con.execute("SELECT COUNT(*) FROM operators WHERE operator_class IS NOT NULL").fetchone()[0]
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


# --------------------------------------------------------------------------- #
# Phase 4 - geocode via US Census batch geocoder + content-addressed cache
# --------------------------------------------------------------------------- #

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
    """Deterministically ordered list of distinct geocodable address tuples.

    Each tuple is run through addr_key() so it is byte-identical to the cache
    key form (NULL components normalized to '', zip truncated to 5). This keeps
    the cache diff, the geocode results, and the final DB join all keyed the
    same way - otherwise a row with a NULL city/state/zip would never cache-hit.
    """
    con = sqlite3.connect(db_path)
    sql = """
        SELECT DISTINCT
               UPPER(TRIM(street_address)),
               UPPER(TRIM(city)),
               UPPER(TRIM(state)),
               SUBSTR(TRIM(zip_code), 1, 5)
        FROM operators
        WHERE street_address IS NOT NULL AND TRIM(street_address) != ''
        ORDER BY 1, 2, 3, 4
    """
    rows = con.execute(sql).fetchall()
    con.close()
    seen, out = set(), []
    for r in rows:
        k = addr_key(*r)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def open_cache(cache_dir):
    """Open (creating if needed) the persistent content-addressed cache."""
    con = sqlite3.connect(os.path.join(cache_dir, CACHE_DB))
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
    for street, city, state, zip5, lat, lon, quality, matched, fetched_at in con.execute(
        "SELECT street, city, state, zip5, lat, lon, quality, matched, fetched_at "
        "FROM geocode_cache"
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
            # Interruptible sleep: a Ctrl-C during a 300s backoff should not
            # have to wait it out.
            if _INTERRUPTED.wait(wait):
                return False
    return False


def parse_result_csv(path):
    """Return {int_id: (lat, lon, quality, matched)} for every row (hit or miss).

    Census output row: id, input_address, Match|No_Match|Tie
    [, Exact|Non_Exact, matched_address, "lon,lat", tigerline_id, side].
    Coordinates come back LONGITUDE-FIRST and are swapped to lat,lon here.
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


def geocode_todo(con_cache, todo, cache_dir, workers, now):
    """Geocode the queued addresses, writing each parsed batch into the cache."""
    work_dir = os.path.join(cache_dir, "_work")
    os.makedirs(work_dir, exist_ok=True)
    # purge orphans from a previous crashed/stopped run: the cache is the only
    # resume state, so anything in _work is dead weight (and files numbered
    # beyond this run's batch count would otherwise linger forever)
    for f in os.listdir(work_dir):
        try:
            os.remove(os.path.join(work_dir, f))
        except OSError:
            pass

    batches = []  # (bno, start, csv_path, result_path)
    for bno, start in enumerate(range(0, len(todo), BATCH_SIZE)):
        chunk = todo[start : start + BATCH_SIZE]
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
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {
            pool.submit(submit_batch, cp, rp): (bno, cp, rp)
            for bno, start, cp, rp in batches
        }
        for fut in as_completed(futs):
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
        # Every completed batch is already committed to the cache, so there is
        # nothing to save here - just stop the queued batches from starting and
        # let __main__ exit.
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


def geocode_database(db, cache_dir, workers, miss_retry_days):
    """Phase 4 driver: cache diff -> Census batches -> UPDATE operators."""
    log("Extracting distinct addresses...")
    addresses = extract_distinct_addresses(db)
    log(f"{len(addresses)} distinct geocodable addresses")

    os.makedirs(cache_dir, exist_ok=True)
    con_cache = open_cache(cache_dir)
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
        failed = geocode_todo(con_cache, todo, cache_dir, workers, now)
        if failed:
            con_cache.close()
            sys.exit(f"ERROR: batches failed permanently: {sorted(failed)} - "
                     f"rerun to retry (their addresses stay uncached)")
    else:
        log("Nothing new to geocode; using cache as-is.")

    # rebuild address -> coordinate map from the cache
    cache = load_cache(con_cache)
    con_cache.close()
    geo = {
        k: (v[1], v[2], v[3]) for k, v in cache.items() if v[0]  # matched only
    }
    matched = sum(1 for a in addresses if a in geo)
    log(f"{matched}/{len(addresses)} distinct addresses matched "
        f"({matched / max(len(addresses), 1):.1%})")

    log("Updating database with street-level coordinates + gridsquares...")
    con = sqlite3.connect(db)
    updates = []
    rows_matched = 0
    cur = con.execute(
        "SELECT unique_system_identifier, street_address, city, state, zip_code FROM operators"
    )
    for pk, street, city, state, zipc in cur:
        hit = geo.get(addr_key(street, city, state, zipc))
        if hit:
            lat, lon, qual = hit
            gs = maidenhead4(lat, lon)
            updates.append((f"{lat:.6f},{lon:.6f}", gs, qual, pk))
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


# --------------------------------------------------------------------------- #
# Census reference files
#
# Two files, one job: have the right one on disk, and say something when a
# newer one exists. The vintage in use is PINNED in REFERENCES above and the
# local file is named after it, so the filename alone answers "which vintage is
# this?" - no metadata table, no checksums, no conditional requests.
#
#   1. The pinned file is already here    -> use it.
#   2. It is not                          -> download it.
#   3. The download fails, but an older
#      vintage of the same file is here   -> use that, loudly.
#   4. A newer vintage exists upstream    -> log a banner saying which line of
#                                            this file to edit. Never adopted
#                                            automatically: county NAME feeds
#                                            both `county` and `arrl_section`,
#                                            so a resplit county silently
#                                            changes results.
#
# Adopting a new vintage is therefore a code edit and a rerun, which is also
# the record of when it happened. Everything here is advisory except case 3
# failing outright: no local file and no download means Phase 5/6 cannot run.
# --------------------------------------------------------------------------- #

def _ref_url(spec, vintage):
    return spec["url_template"].format(y=vintage)


def _ref_path(cache_dir, spec, vintage):
    return os.path.join(cache_dir, spec["filename_template"].format(y=vintage))


def _local_vintages(cache_dir, spec):
    """Vintages of this reference file present in the cache dir, newest first."""
    pat = re.escape(spec["filename_template"]).replace(r"\{y\}", r"(\d{4})")
    out = []
    for name in os.listdir(cache_dir):
        m = re.fullmatch(pat, name)
        if m:
            out.append(int(m.group(1)))
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
    # Prove it before it takes the real name: a truncated 200 would otherwise
    # sit there looking exactly like a good file until Phase 5 or 6 opened it.
    try:
        with zipfile.ZipFile(tmp) as zf:
            if zf.testzip() is not None:
                raise RuntimeError("corrupt member")
    except Exception:
        os.remove(tmp)
        raise
    os.replace(tmp, path)
    log(f"{spec['desc']}: {len(resp.content):,} bytes -> {os.path.basename(path)}")


def _probe_newer(spec, vintage):
    """Highest published vintage above `vintage`, or None. Advisory only."""
    newest = None
    for y in range(vintage + 1, time.gmtime().tm_year + 2):
        try:
            r = requests.head(_ref_url(spec, y), timeout=(10, 30),
                              allow_redirects=True, headers=HTTP_HEADERS)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            newest = y
    return newest


def ensure_reference(cache_dir, key, ref_opts):
    """Return a local path to the reference file, downloading it if needed."""
    spec = REFERENCES[key]
    want = spec["vintage"]
    os.makedirs(cache_dir, exist_ok=True)
    path = _ref_path(cache_dir, spec, want)

    if os.path.exists(path):
        log(f"{spec['desc']}: vintage {want} ({os.path.basename(path)})")
    else:
        try:
            _download_ref(spec, want, path)
            for old in _local_vintages(cache_dir, spec):
                if old != want:
                    try:
                        os.remove(_ref_path(cache_dir, spec, old))
                        log(f"  removed superseded vintage {old}")
                    except OSError:
                        pass
        except Exception as e:
            # Fall back to any older copy rather than losing Phase 5/6 entirely.
            older = [v for v in _local_vintages(cache_dir, spec) if v != want]
            if not older:
                sys.exit(f"ERROR: cannot obtain {spec['desc']} vintage {want} "
                         f"({e}) and no older copy exists in {cache_dir}")
            path = _ref_path(cache_dir, spec, older[0])
            log_banner([
                f" NOTE: falling back to an older {spec['desc']}",
                "",
                f"   vintage {want} could not be downloaded:",
                f"     {e}",
                "",
                f"   Using vintage {older[0]} already in the cache instead.",
                "   Results will reflect that older vintage.",
            ])
            return path

    if not ref_opts["check"]:
        return path

    # Checked every run, not on a timer: an upgrade you have not taken yet
    # needs to keep announcing itself. Costs a couple of HEAD requests.
    try:
        newer = _probe_newer(spec, want)
    except requests.RequestException as e:
        log(f"WARNING: {spec['desc']} freshness check failed ({e})")
        return path
    if newer:
        log_banner([
            f" NOTE: a newer {spec['desc']} release is available",
            "",
            f"   using:      vintage {want}",
            f"   available:  vintage {newer}",
            "",
            *[f"   {line}" for line in spec["why_manual"]],
            "",
            f"   To take it, edit REFERENCES[{key!r}]['vintage'] in",
            f"   {os.path.basename(__file__)} to {newer} and rerun.",
        ])
    else:
        log(f"{spec['desc']}: vintage {want} is current")
    return path



# --------------------------------------------------------------------------- #
# Phase 5 - ZIP-centroid fallback (ZCTA Gazetteer)
# --------------------------------------------------------------------------- #

def load_zip_centroids(cache_dir, ref_opts):
    """{zip5: (lat, lon)} from the Census ZCTA Gazetteer."""
    zip_path = ensure_reference(cache_dir, GAZETTEER_KEY, ref_opts)
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


def apply_zip_fallback(db, cache_dir, ref_opts):
    """Fill coordinates from ZIP centroids for rows the street geocode missed."""
    centroids = load_zip_centroids(cache_dir, ref_opts)
    con = sqlite3.connect(db)
    updates = []
    cur = con.execute(
        "SELECT unique_system_identifier, zip_code FROM operators "
        "WHERE coordinates IS NULL AND zip_code IS NOT NULL"
    )
    # ZCTA codes grouped by 3-digit prefix for the nearest-ZIP approximation:
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
        best = min(cands, key=lambda c: abs(c - int(zip5)))
        return f"{best:05d}"

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


# --------------------------------------------------------------------------- #
# Phase 6 - county assignment (point-in-polygon vs Census county boundaries)
# --------------------------------------------------------------------------- #

def load_county_shapes(cache_dir, ref_opts):
    """(geometries, names, states) from the Census county boundary file.

    Uses the 1:500,000-scale generalized file (~11 MB). `names` holds the short
    NAME field ("Jefferson"), not NAMELSAD ("Jefferson Parish"), per the schema
    contract for `county`. `states` holds the STUSPS code ("CA") so Phase 6 can
    confine each point to counties of the licensee's own state.
    """
    import shapefile as pyshp
    from shapely.geometry import shape as shapely_shape

    zip_path = ensure_reference(cache_dir, COUNTY_KEY, ref_opts)
    with zipfile.ZipFile(zip_path) as zf:
        # Member names come from the archive rather than being derived from the
        # local filename, so a Census change to the internal naming does not
        # need one here (cb_2025_us_county_500k.shp).
        shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
        base = shp_name[:-4]
        rdr = pyshp.Reader(
            shp=io.BytesIO(zf.read(base + ".shp")),
            shx=io.BytesIO(zf.read(base + ".shx")),
            dbf=io.BytesIO(zf.read(base + ".dbf")),
        )
        # STUSPS has been in this file since the 2019 vintage, so this is a
        # tripwire rather than an expected path - but failing here beats a
        # KeyError 40 minutes into a run.
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


# The line between "snap this point to the coast it just missed" and "refuse to
# guess". A snap this far from the nearest county of the row's own state is not
# a coastal correction: the coordinate itself is wrong, usually a mistyped ZIP
# that sent Phase 5 to the wrong end of the country. Degrees, ~111 km each.
#
# Measured against the full dataset, the two populations barely overlap: of 241
# points outside every polygon of their own state, 121 sit within 0.25 deg
# (true coastal misses), 6 fall in 0.25-0.5, and 114 are beyond 0.5 - the
# nearest being 56 km out and the worst 268 deg, a Guam ZIP on a California
# address. Anything past this line is dropped rather than snapped: a NULL
# county is honest, while a snapped one is indistinguishable from a real
# lookup and silently poisons `arrl_section` too.
FAR_SNAP_DEGREES = 0.5


def assign_counties(db, cache_dir, ref_opts):
    """Fill `county` for every row with coordinates, within its own state.

    Distinct (coordinates, state) pairs are resolved once (ZIP-centroid rows
    share points) via a bulk STRtree containment query against the counties of
    that state ONLY - a geocode landing just across a state line would
    otherwise pick up the neighbouring state's county, which is both wrong in
    `county` and unmappable in the Phase 9 section lookup. Points inside no
    polygon of their own state - street matches geocoded slightly offshore,
    coastal ZIP centroids - snap to the nearest county in that same state,
    mirroring the Zip_Approx philosophy, but ONLY within FAR_SNAP_DEGREES:
    past that the coordinate is wrong rather than merely offshore, and the row
    is left NULL instead of being given an invented county. Rows whose state
    has no county polygons at all (UM, foreign codes) are left NULL too: there
    is no US county-equivalent to give them.
    """
    import shapely
    from shapely.strtree import STRtree

    geoms, names, states = load_county_shapes(cache_dir, ref_opts)

    # One tree per state: the same 3,235 polygons, partitioned so a point can
    # only ever match a county of its own state. 56 small trees also query
    # marginally faster than one big one, so this costs nothing.
    by_state = {}
    for g, nm, st in zip(geoms, names, states):
        gs, ns = by_state.setdefault(st, ([], []))
        gs.append(g)
        ns.append(nm)
    trees = {st: (STRtree(gs), gs, ns) for st, (gs, ns) in by_state.items()}

    con = sqlite3.connect(db)
    pairs = con.execute(
        "SELECT DISTINCT coordinates, state FROM operators "
        "WHERE coordinates IS NOT NULL AND county IS NULL"
    ).fetchall()
    if not pairs:
        con.close()
        log("County: nothing to assign.")
        return
    # Keying on (coordinates, state) rather than coordinates alone costs
    # almost nothing - measured 658,430 pairs vs 658,362 distinct coordinates.
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
            # UM (US Minor Outlying Islands) and any foreign/malformed code:
            # no US county-equivalent exists, so leave it NULL rather than
            # snapping to whichever mainland county happens to be closest.
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
                # Too far to be a coastal miss. The county IS in the licensee's
                # state, but the point is nowhere near it, so the coordinate is
                # the thing to distrust - leave county NULL rather than invent
                # one that reads exactly like a real point-in-polygon hit.
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

    # update by primary key: coordinates is unindexed, so updating by it
    # would full-scan the table per distinct coordinate
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
    n = con.execute("SELECT COUNT(*) FROM operators WHERE county IS NOT NULL").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    con.close()
    log(f"County: {n:,}/{total:,} rows assigned "
        f"({snapped} point(s) snapped to the nearest county in their state).")


# --------------------------------------------------------------------------- #
# Phase 7 - DXCC entity (US-affiliated territories vs. the continental US)
# --------------------------------------------------------------------------- #

# DXCC entity is determined by the station's physical location, NOT its
# callsign (US callsigns are portable - a KH6 prefix can be held from Ohio and
# a mainland W-call from Hawaii; measured agreement is only ~83-93%). The
# licensee's state code is therefore the correct key.

# USPS/FCC state code -> (ARRL DXCC entity name, ARRL DXCC entity number).
DXCC_BY_STATE = {
    "AK": ("Alaska", 6),
    "HI": ("Hawaii", 110),
    "PR": ("Puerto Rico", 202),
    "VI": ("US Virgin Islands", 285),
    "GU": ("Guam", 103),
    "MP": ("Northern Mariana Islands", 166),
    "AS": ("American Samoa", 9),
}
# The 48 contiguous states + DC are the single DXCC entity "United States".
CONTIGUOUS_STATES = {
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR",
    "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI",
    "WY",
}
# APO/FPO military mail: the station could be physically anywhere, so no single
# DXCC entity applies.
DXCC_MILITARY = {"AA", "AE", "AP"}


def dxcc_for_state(state):
    """(entity_name, dxcc_id) for a USPS state/territory code.

    Case-insensitive (the raw FCC data has 'Fl', 'az', etc.). Returns
    ('Military (APO/FPO)', None) for AA/AE/AP and (None, None) for anything
    undeterminable (US Minor Outlying Is., foreign, blank, malformed).
    """
    st = (state or "").strip().upper()
    if st in CONTIGUOUS_STATES:
        return ("United States", 291)
    if st in DXCC_BY_STATE:
        return DXCC_BY_STATE[st]
    if st in DXCC_MILITARY:
        return ("Military (APO/FPO)", None)
    return (None, None)


def assign_dxcc(db):
    """Fill dxcc_entity / dxcc_id from each row's (normalized) state code."""
    con = sqlite3.connect(db)
    # only a few dozen distinct state strings; resolve each once, then bulk
    # UPDATE by exact stored value (`IS` is NULL-safe, so blank states match)
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


# --------------------------------------------------------------------------- #
# Phase 8 - continent (NA / OC lookup table)
# --------------------------------------------------------------------------- #

# ARRL DXCC entity number -> continent, keyed on the entity assigned in Phase 7
# (i.e. the station's physical location, not its callsign). Only two continents
# occur in the US amateur file: North America (NA) and Oceania (OC). Puerto Rico
# and the US Virgin Islands are Caribbean = North America; Hawaii and the three
# Pacific island territories are Oceania. APO/FPO military mail (dxcc_id NULL)
# and undeterminable rows are neither, so they get no row here.
CONTINENT_BY_DXCC_ID = {
    291: "NA",   # United States (48 contiguous + DC)
    6:   "NA",   # Alaska
    202: "NA",   # Puerto Rico
    285: "NA",   # US Virgin Islands
    110: "OC",   # Hawaii
    103: "OC",   # Guam
    166: "OC",   # Northern Mariana Islands
    9:   "OC",   # American Samoa
}


def assign_continent(db):
    """Fill the `continent` column ('NA' / 'OC') from each row's dxcc_id.

    Only North America (NA) and Oceania (OC) occur in the US amateur file.
    APO/FPO military and undeterminable rows have no dxcc_id, so their continent
    stays NULL. Reads dxcc_id, so Phase 7 must have run (main skips this phase
    when --no-dxcc is given, since every row would otherwise resolve to NULL).
    """
    con = sqlite3.connect(db)
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


# --------------------------------------------------------------------------- #
# Phase 9 - ARRL Section (state + county for split states)
# --------------------------------------------------------------------------- #

# Most US states/territories map 1:1 to an ARRL section whose abbreviation
# equals the USPS state code. Exceptions: MD+DC merge to "MDC", HI and the
# Pacific territories merge to "PAC", and 8 states are split into multiple
# sections along county lines. Military APO/FPO codes have no section.

SECTION_BY_STATE = {
    "AL": "AL",  "AZ": "AZ",  "AR": "AR",  "CO": "CO",  "CT": "CT",
    "DE": "DE",  "GA": "GA",  "ID": "ID",  "IL": "IL",  "IN": "IN",
    "IA": "IA",  "KS": "KS",  "KY": "KY",  "LA": "LA",  "ME": "ME",
    "MI": "MI",  "MN": "MN",  "MS": "MS",  "MO": "MO",  "MT": "MT",
    "NE": "NE",  "NV": "NV",  "NH": "NH",  "NM": "NM",  "NC": "NC",
    "ND": "ND",  "OH": "OH",  "OK": "OK",  "OR": "OR",  "RI": "RI",
    "SC": "SC",  "SD": "SD",  "TN": "TN",  "UT": "UT",  "VT": "VT",
    "VA": "VA",  "WV": "WV",  "WI": "WI",  "WY": "WY",
    "MD": "MDC", "DC": "MDC",
    "HI": "PAC", "GU": "PAC", "AS": "PAC", "MP": "PAC",
    "AK": "AK",  "PR": "PR",  "VI": "VI",
}

# county NAME (as stored by Phase 6 from the Census shapefile) -> section
# for the 8 states split into multiple ARRL sections.
SECTION_BY_COUNTY = {
    "CA": {
        "Alameda": "EB", "Contra Costa": "EB", "Napa": "EB", "Solano": "EB",
        "Los Angeles": "LAX",
        "Inyo": "ORG", "Orange": "ORG", "Riverside": "ORG",
        "San Bernardino": "ORG",
        "San Luis Obispo": "SB", "Santa Barbara": "SB", "Ventura": "SB",
        "Monterey": "SCV", "San Benito": "SCV", "San Mateo": "SCV",
        "Santa Clara": "SCV", "Santa Cruz": "SCV",
        "Imperial": "SDG", "San Diego": "SDG",
        "Del Norte": "SF", "Humboldt": "SF", "Lake": "SF", "Marin": "SF",
        "Mendocino": "SF", "San Francisco": "SF", "Sonoma": "SF",
        "Calaveras": "SJV", "Fresno": "SJV", "Kern": "SJV", "Kings": "SJV",
        "Madera": "SJV", "Mariposa": "SJV", "Merced": "SJV", "Mono": "SJV",
        "San Joaquin": "SJV", "Stanislaus": "SJV", "Tulare": "SJV",
        "Tuolumne": "SJV",
        "Alpine": "SV", "Amador": "SV", "Butte": "SV", "Colusa": "SV",
        "El Dorado": "SV", "Glenn": "SV", "Lassen": "SV", "Modoc": "SV",
        "Nevada": "SV", "Placer": "SV", "Plumas": "SV", "Sacramento": "SV",
        "Shasta": "SV", "Sierra": "SV", "Siskiyou": "SV", "Sutter": "SV",
        "Tehama": "SV", "Trinity": "SV", "Yolo": "SV", "Yuba": "SV",
    },
    "FL": {
        "Alachua": "NFL", "Baker": "NFL", "Bay": "NFL", "Bradford": "NFL",
        "Calhoun": "NFL", "Citrus": "NFL", "Clay": "NFL", "Columbia": "NFL",
        "Dixie": "NFL", "Duval": "NFL", "Escambia": "NFL", "Flagler": "NFL",
        "Franklin": "NFL", "Gadsden": "NFL", "Gilchrist": "NFL",
        "Gulf": "NFL", "Hamilton": "NFL", "Hernando": "NFL", "Holmes": "NFL",
        "Jackson": "NFL", "Jefferson": "NFL", "Lafayette": "NFL",
        "Lake": "NFL", "Leon": "NFL", "Levy": "NFL", "Liberty": "NFL",
        "Madison": "NFL", "Marion": "NFL", "Nassau": "NFL",
        "Okaloosa": "NFL", "Orange": "NFL", "Putnam": "NFL",
        "Santa Rosa": "NFL", "Seminole": "NFL", "St. Johns": "NFL",
        "Sumter": "NFL", "Suwannee": "NFL", "Taylor": "NFL", "Union": "NFL",
        "Volusia": "NFL", "Wakulla": "NFL", "Walton": "NFL",
        "Washington": "NFL",
        "Brevard": "SFL", "Broward": "SFL", "Collier": "SFL",
        "Miami-Dade": "SFL", "Glades": "SFL", "Hendry": "SFL",
        "Indian River": "SFL", "Lee": "SFL", "Martin": "SFL",
        "Monroe": "SFL", "Okeechobee": "SFL", "Osceola": "SFL",
        "Palm Beach": "SFL", "St. Lucie": "SFL",
        "Charlotte": "WCF", "DeSoto": "WCF", "Hardee": "WCF",
        "Highlands": "WCF", "Hillsborough": "WCF", "Manatee": "WCF",
        "Pasco": "WCF", "Pinellas": "WCF", "Polk": "WCF", "Sarasota": "WCF",
    },
    "MA": {
        "Barnstable": "EMA", "Bristol": "EMA", "Dukes": "EMA",
        "Essex": "EMA", "Middlesex": "EMA", "Nantucket": "EMA",
        "Norfolk": "EMA", "Plymouth": "EMA", "Suffolk": "EMA",
        "Berkshire": "WMA", "Franklin": "WMA", "Hampden": "WMA",
        "Hampshire": "WMA", "Worcester": "WMA",
    },
    "NJ": {
        "Bergen": "NNJ", "Essex": "NNJ", "Hudson": "NNJ",
        "Hunterdon": "NNJ", "Middlesex": "NNJ", "Monmouth": "NNJ",
        "Morris": "NNJ", "Passaic": "NNJ", "Somerset": "NNJ",
        "Sussex": "NNJ", "Union": "NNJ", "Warren": "NNJ",
        "Atlantic": "SNJ", "Burlington": "SNJ", "Camden": "SNJ",
        "Cape May": "SNJ", "Cumberland": "SNJ", "Gloucester": "SNJ",
        "Mercer": "SNJ", "Ocean": "SNJ", "Salem": "SNJ",
    },
    "NY": {
        "Albany": "ENY", "Columbia": "ENY", "Dutchess": "ENY",
        "Greene": "ENY", "Orange": "ENY", "Putnam": "ENY",
        "Rensselaer": "ENY", "Rockland": "ENY", "Saratoga": "ENY",
        "Schenectady": "ENY", "Sullivan": "ENY", "Ulster": "ENY",
        "Warren": "ENY", "Washington": "ENY", "Westchester": "ENY",
        "Bronx": "NLI", "Kings": "NLI", "Nassau": "NLI", "New York": "NLI",
        "Queens": "NLI", "Richmond": "NLI", "Suffolk": "NLI",
        "Clinton": "NNY", "Essex": "NNY", "Franklin": "NNY",
        "Fulton": "NNY", "Hamilton": "NNY", "Jefferson": "NNY",
        "Lewis": "NNY", "Montgomery": "NNY", "St. Lawrence": "NNY",
        "Schoharie": "NNY",
        "Allegany": "WNY", "Broome": "WNY", "Cattaraugus": "WNY",
        "Cayuga": "WNY", "Chautauqua": "WNY", "Chemung": "WNY",
        "Chenango": "WNY", "Cortland": "WNY", "Delaware": "WNY",
        "Erie": "WNY", "Genesee": "WNY", "Herkimer": "WNY",
        "Livingston": "WNY", "Madison": "WNY", "Monroe": "WNY",
        "Niagara": "WNY", "Oneida": "WNY", "Onondaga": "WNY",
        "Ontario": "WNY", "Orleans": "WNY", "Oswego": "WNY",
        "Otsego": "WNY", "Schuyler": "WNY", "Seneca": "WNY",
        "Steuben": "WNY", "Tioga": "WNY", "Tompkins": "WNY",
        "Wayne": "WNY", "Wyoming": "WNY", "Yates": "WNY",
    },
    "PA": {
        "Adams": "EPA", "Berks": "EPA", "Bradford": "EPA", "Bucks": "EPA",
        "Carbon": "EPA", "Chester": "EPA", "Columbia": "EPA",
        "Cumberland": "EPA", "Dauphin": "EPA", "Delaware": "EPA",
        "Juniata": "EPA", "Lackawanna": "EPA", "Lancaster": "EPA",
        "Lebanon": "EPA", "Lehigh": "EPA", "Luzerne": "EPA",
        "Lycoming": "EPA", "Monroe": "EPA", "Montgomery": "EPA",
        "Montour": "EPA", "Northampton": "EPA", "Northumberland": "EPA",
        "Perry": "EPA", "Philadelphia": "EPA", "Pike": "EPA",
        "Schuylkill": "EPA", "Snyder": "EPA", "Sullivan": "EPA",
        "Susquehanna": "EPA", "Tioga": "EPA", "Union": "EPA",
        "Wayne": "EPA", "Wyoming": "EPA", "York": "EPA",
        "Allegheny": "WPA", "Armstrong": "WPA", "Beaver": "WPA",
        "Bedford": "WPA", "Blair": "WPA", "Butler": "WPA",
        "Cambria": "WPA", "Cameron": "WPA", "Centre": "WPA",
        "Clarion": "WPA", "Clearfield": "WPA", "Clinton": "WPA",
        "Crawford": "WPA", "Elk": "WPA", "Erie": "WPA", "Fayette": "WPA",
        "Forest": "WPA", "Franklin": "WPA", "Fulton": "WPA",
        "Greene": "WPA", "Huntingdon": "WPA", "Indiana": "WPA",
        "Jefferson": "WPA", "Lawrence": "WPA", "McKean": "WPA",
        "Mercer": "WPA", "Mifflin": "WPA", "Potter": "WPA",
        "Somerset": "WPA", "Venango": "WPA", "Warren": "WPA",
        "Washington": "WPA", "Westmoreland": "WPA",
    },
    "TX": {
        "Anderson": "NTX", "Archer": "NTX", "Baylor": "NTX",
        "Bell": "NTX", "Bosque": "NTX", "Bowie": "NTX", "Brown": "NTX",
        "Camp": "NTX", "Cass": "NTX", "Cherokee": "NTX", "Clay": "NTX",
        "Collin": "NTX", "Comanche": "NTX", "Cooke": "NTX",
        "Coryell": "NTX", "Dallas": "NTX", "Delta": "NTX",
        "Denton": "NTX", "Eastland": "NTX", "Ellis": "NTX",
        "Erath": "NTX", "Falls": "NTX", "Fannin": "NTX",
        "Franklin": "NTX", "Freestone": "NTX", "Grayson": "NTX",
        "Gregg": "NTX", "Hamilton": "NTX", "Harrison": "NTX",
        "Henderson": "NTX", "Hill": "NTX", "Hood": "NTX",
        "Hopkins": "NTX", "Hunt": "NTX", "Jack": "NTX", "Johnson": "NTX",
        "Kaufman": "NTX", "Lamar": "NTX", "Lampasas": "NTX",
        "Limestone": "NTX", "McLennan": "NTX", "Marion": "NTX",
        "Mills": "NTX", "Montague": "NTX", "Morris": "NTX",
        "Nacogdoches": "NTX", "Navarro": "NTX", "Palo Pinto": "NTX",
        "Panola": "NTX", "Parker": "NTX", "Rains": "NTX",
        "Red River": "NTX", "Rockwall": "NTX", "Rusk": "NTX",
        "Shelby": "NTX", "Smith": "NTX", "Somervell": "NTX",
        "Stephens": "NTX", "Tarrant": "NTX", "Throckmorton": "NTX",
        "Titus": "NTX", "Upshur": "NTX", "Van Zandt": "NTX",
        "Wichita": "NTX", "Wilbarger": "NTX", "Wise": "NTX",
        "Wood": "NTX", "Young": "NTX",
        "Angelina": "STX", "Aransas": "STX", "Atascosa": "STX",
        "Austin": "STX", "Bandera": "STX", "Bastrop": "STX", "Bee": "STX",
        "Bexar": "STX", "Blanco": "STX", "Brazoria": "STX",
        "Brazos": "STX", "Brooks": "STX", "Burleson": "STX",
        "Burnet": "STX", "Caldwell": "STX", "Calhoun": "STX",
        "Cameron": "STX", "Chambers": "STX", "Colorado": "STX",
        "Comal": "STX", "Concho": "STX", "DeWitt": "STX",
        "Dimmit": "STX", "Duval": "STX", "Edwards": "STX",
        "Fayette": "STX", "Fort Bend": "STX", "Frio": "STX",
        "Galveston": "STX", "Gillespie": "STX", "Goliad": "STX",
        "Gonzales": "STX", "Grimes": "STX", "Guadalupe": "STX",
        "Hardin": "STX", "Harris": "STX", "Hays": "STX",
        "Hidalgo": "STX", "Houston": "STX", "Jackson": "STX",
        "Jasper": "STX", "Jefferson": "STX", "Jim Hogg": "STX",
        "Jim Wells": "STX", "Karnes": "STX", "Kendall": "STX",
        "Kenedy": "STX", "Kerr": "STX", "Kimble": "STX",
        "Kinney": "STX", "Kleberg": "STX", "La Salle": "STX",
        "Lavaca": "STX", "Lee": "STX", "Leon": "STX", "Liberty": "STX",
        "Live Oak": "STX", "Llano": "STX", "Madison": "STX",
        "Mason": "STX", "Matagorda": "STX", "Maverick": "STX",
        "McCulloch": "STX", "McMullen": "STX", "Medina": "STX",
        "Menard": "STX", "Milam": "STX", "Montgomery": "STX",
        "Newton": "STX", "Nueces": "STX", "Orange": "STX", "Polk": "STX",
        "Real": "STX", "Refugio": "STX", "Robertson": "STX",
        "Sabine": "STX", "San Augustine": "STX", "San Jacinto": "STX",
        "San Patricio": "STX", "San Saba": "STX", "Starr": "STX",
        "Travis": "STX", "Trinity": "STX", "Tyler": "STX",
        "Uvalde": "STX", "Val Verde": "STX", "Victoria": "STX",
        "Walker": "STX", "Waller": "STX", "Washington": "STX",
        "Webb": "STX", "Wharton": "STX", "Willacy": "STX",
        "Williamson": "STX", "Wilson": "STX", "Zapata": "STX",
        "Zavala": "STX",
        "Andrews": "WTX", "Armstrong": "WTX", "Bailey": "WTX",
        "Borden": "WTX", "Brewster": "WTX", "Briscoe": "WTX",
        "Callahan": "WTX", "Carson": "WTX", "Castro": "WTX",
        "Childress": "WTX", "Cochran": "WTX", "Coke": "WTX",
        "Coleman": "WTX", "Collingsworth": "WTX", "Cottle": "WTX",
        "Crane": "WTX", "Crockett": "WTX", "Crosby": "WTX",
        "Culberson": "WTX", "Dallam": "WTX", "Dawson": "WTX",
        "Deaf Smith": "WTX", "Dickens": "WTX", "Donley": "WTX",
        "Ector": "WTX", "El Paso": "WTX", "Fisher": "WTX",
        "Floyd": "WTX", "Foard": "WTX", "Gaines": "WTX", "Garza": "WTX",
        "Glasscock": "WTX", "Gray": "WTX", "Hale": "WTX", "Hall": "WTX",
        "Hansford": "WTX", "Hardeman": "WTX", "Hartley": "WTX",
        "Haskell": "WTX", "Hemphill": "WTX", "Hockley": "WTX",
        "Howard": "WTX", "Hudspeth": "WTX", "Hutchinson": "WTX",
        "Irion": "WTX", "Jeff Davis": "WTX", "Jones": "WTX",
        "Kent": "WTX", "King": "WTX", "Knox": "WTX", "Lamb": "WTX",
        "Lipscomb": "WTX", "Loving": "WTX", "Lubbock": "WTX",
        "Lynn": "WTX", "Martin": "WTX", "Midland": "WTX",
        "Mitchell": "WTX", "Moore": "WTX", "Motley": "WTX",
        "Nolan": "WTX", "Ochiltree": "WTX", "Oldham": "WTX",
        "Parmer": "WTX", "Pecos": "WTX", "Potter": "WTX",
        "Presidio": "WTX", "Randall": "WTX", "Reagan": "WTX",
        "Reeves": "WTX", "Roberts": "WTX", "Runnels": "WTX",
        "Schleicher": "WTX", "Scurry": "WTX", "Shackelford": "WTX",
        "Sherman": "WTX", "Sterling": "WTX", "Stonewall": "WTX",
        "Sutton": "WTX", "Swisher": "WTX", "Taylor": "WTX",
        "Terrell": "WTX", "Terry": "WTX", "Tom Green": "WTX",
        "Upton": "WTX", "Ward": "WTX", "Wheeler": "WTX",
        "Winkler": "WTX", "Yoakum": "WTX",
    },
    "WA": {
        "Adams": "EWA", "Asotin": "EWA", "Benton": "EWA",
        "Chelan": "EWA", "Columbia": "EWA", "Douglas": "EWA",
        "Ferry": "EWA", "Franklin": "EWA", "Garfield": "EWA",
        "Grant": "EWA", "Kittitas": "EWA", "Klickitat": "EWA",
        "Lincoln": "EWA", "Okanogan": "EWA", "Pend Oreille": "EWA",
        "Spokane": "EWA", "Stevens": "EWA", "Walla Walla": "EWA",
        "Whitman": "EWA", "Yakima": "EWA",
        "Clallam": "WWA", "Clark": "WWA", "Cowlitz": "WWA",
        "Grays Harbor": "WWA", "Island": "WWA", "Jefferson": "WWA",
        "King": "WWA", "Kitsap": "WWA", "Lewis": "WWA", "Mason": "WWA",
        "Pacific": "WWA", "Pierce": "WWA", "San Juan": "WWA",
        "Skagit": "WWA", "Skamania": "WWA", "Snohomish": "WWA",
        "Thurston": "WWA", "Wahkiakum": "WWA", "Whatcom": "WWA",
    },
}

SPLIT_STATES = set(SECTION_BY_COUNTY)
SECTION_MILITARY = {"AA", "AE", "AP"}


def section_for(state, county):
    """ARRL section abbreviation for a (state, county) pair.

    Non-split states resolve from state alone. Split states require
    county (the Census NAME field from Phase 6). Returns None for
    military, foreign, blank, or unmappable rows.
    """
    st = (state or "").strip().upper()
    if st in SECTION_MILITARY:
        return None
    if st in SPLIT_STATES:
        return SECTION_BY_COUNTY[st].get(county)
    return SECTION_BY_STATE.get(st)


def assign_sections(db):
    """Fill `arrl_section` from state (and county for split states)."""
    con = sqlite3.connect(db)
    # --- validate county tables against actual DB data ---
    for st, county_map in SECTION_BY_COUNTY.items():
        db_counties = {r[0] for r in con.execute(
            "SELECT DISTINCT county FROM operators "
            "WHERE state=? AND county IS NOT NULL", (st,)
        )}
        table_counties = set(county_map)

        unmapped = db_counties - table_counties
        if unmapped:
            # cross-state geocode snaps (address in one state, coordinates
            # landed in a neighboring state's county) are expected for a
            # handful of rows; these stay NULL and are not bugs in the table
            log(f"  note: {st} has {len(unmapped)} cross-state county name(s) "
                f"(will be NULL): {sorted(unmapped)}")
        unused = table_counties - db_counties
        if unused:
            log(f"  note: {st} has {len(unused)} county name(s) in table "
                f"not seen in DB: {sorted(unused)}")

    # --- non-split states: bulk update by distinct state ---
    states = [r[0] for r in con.execute("SELECT DISTINCT state FROM operators")]
    non_split = [(section_for(st, None), st) for st in states
                 if (st or "").strip().upper() not in SPLIT_STATES]
    con.executemany(
        "UPDATE operators SET arrl_section=? WHERE state IS ?",
        non_split,
    )

    # --- split states: update by (state, county) ---
    for st in SPLIT_STATES:
        pairs = con.execute(
            "SELECT DISTINCT county FROM operators "
            "WHERE state=? AND county IS NOT NULL", (st,)
        ).fetchall()
        con.executemany(
            "UPDATE operators SET arrl_section=? "
            "WHERE state=? AND county IS ?",
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


# --------------------------------------------------------------------------- #
# Phase 10 - finalize: VACUUM, summary, promote
# --------------------------------------------------------------------------- #

def finalize(db, final_db=None):
    """Report, VACUUM, then promote the work database over the previous one.

    `db` is the working file every phase has been writing to (<final_db>.new);
    `final_db` is the name it takes once it is provably complete. Everything
    before the rename is reversible: if any earlier phase died, the previous
    database is still sitting untouched under `final_db`. Passing final_db=None
    finalizes in place (nothing to promote).
    """
    con = sqlite3.connect(db)

    size_before = os.path.getsize(db)
    log("VACUUM ...")
    con.execute("VACUUM")

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
    n_county = con.execute(
        "SELECT COUNT(*) FROM operators WHERE county IS NOT NULL"
    ).fetchone()[0]
    log(f"  {'with county':>14}: {n_county:>9,} / {total:,} ({n_county / total:.2%})")
    n_section = con.execute(
        "SELECT COUNT(*) FROM operators WHERE arrl_section IS NOT NULL"
    ).fetchone()[0]
    log(f"  {'with section':>14}: {n_section:>9,} / {total:,} ({n_section / total:.2%})")
    con.close()
    log(f"VACUUM: {size_before / 1e6:,.1f} MB -> {os.path.getsize(db) / 1e6:,.1f} MB")

    if final_db is None or os.path.abspath(final_db) == os.path.abspath(db):
        return
    # The one destructive moment in the run, and the last: os.replace is atomic
    # on both POSIX and Windows, so the name never points at a partial file.
    replacing = os.path.exists(final_db)
    try:
        os.replace(db, final_db)
    except OSError as e:
        # Windows refuses to replace a file another process holds open - a
        # sqlite browser pointed at the old database is the usual culprit.
        # The finished database is complete and on disk either way; say where.
        log_banner([
            " NOTE: the finished database could not replace the previous one",
            "",
            f"   {type(e).__name__}: {e}",
            "",
            "   This is normally another process holding the old file open",
            "   (a SQLite browser, an editor, a backup agent).",
            "",
            f"   The new database is COMPLETE and sits at:",
            f"     {db}",
            "",
            "   Close whatever holds the old file and rename it by hand, or",
            "   just rerun once nothing else has it open.",
        ])
        return
    log(f"{'Replaced' if replacing else 'Created'} {final_db}"
        f"{' (previous version discarded)' if replacing else ''}")


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

# Phase 6 imports shapely/pyshp lazily, at the point it needs them - which is
# roughly an hour into a cold run, after the 175 MB download, the build, and
# the whole Census geocode. Discovering a missing package there costs all of
# that work, so check up front instead: the module is only imported to prove
# it is installed, and nothing here does any work.
#
# Only the phases actually enabled are checked, so `--no-county` still runs
# with nothing but `requests` installed.
def preflight(args):
    """Abort before Phase 1 if an enabled phase's packages are missing."""
    needed = {}          # module name -> [phases that need it]
    if not args.no_county:
        for mod in ("shapely", "shapefile"):
            needed.setdefault(mod, []).append("6 (county)")

    missing = []
    for mod, phases in needed.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append((mod, phases))
    if not missing:
        return

    # pip name != import name for pyshp, and the message has to name what you
    # would actually type.
    pip_names = {"shapefile": "pyshp", "shapely": "shapely"}
    pkgs = sorted(pip_names[m] for m, _ in missing)
    sys.exit(
        "ERROR: missing required package(s) for the phases you asked for:\n"
        + "".join(f"  {pip_names[m]:<10} needed by Phase {', '.join(p)}\n"
                  for m, p in sorted(missing))
        + "\nInstall them:\n"
        f"  python -m pip install {' '.join(pkgs)}\n"
        "  (or: python -m pip install -r ../requirements.txt)\n"
        "\nOr skip the phase that needs them:\n"
        "  python update_fcc_db.py --no-county\n"
        "  - county and arrl_section stay NULL in the 8 split states."
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    global _log_fh

    ap = argparse.ArgumentParser(
        description="Full FCC amateur database refresh: cleanup, download, "
                    "build, geocode, gridsquares.")
    ap.add_argument("--cache-dir", default=os.path.join(HERE, "geocode_cache"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--miss-retry-days", type=float, default=30.0,
                    help="re-query a cached miss once it is older than this many "
                         "days (0 = always retry misses)")
    ap.add_argument("--no-zip-fallback", action="store_true",
                    help="skip the ZIP-centroid fallback phase")
    ap.add_argument("--no-county", action="store_true",
                    help="skip the county assignment phase")
    ap.add_argument("--no-dxcc", action="store_true",
                    help="skip the DXCC entity phase")
    ap.add_argument("--no-continent", action="store_true",
                    help="skip the continent (NA/OC) lookup table phase")
    ap.add_argument("--no-section", action="store_true",
                    help="skip the ARRL section assignment phase")
    ap.add_argument("--no-ref-check", action="store_true",
                    help="never check the Census reference files for updates "
                         "(fully offline once they are present)")
    args = ap.parse_args()

    # Before anything is downloaded, built, or geocoded.
    preflight(args)

    # Adopting a newer reference vintage is a code edit (REFERENCES above), not
    # a flag; all that is left is whether to look for one.
    ref_opts = {"check": not args.no_ref_check}

    # Paths are fixed (DB_PATH / WORK_DB / ZIP_PATH, all beside the script).
    work_db, db = WORK_DB, DB_PATH

    _log_fh = open(os.path.join(HERE, RUN_LOG), "w", encoding="utf-8")
    t0 = time.time()
    log(f"=== FCC amateur database refresh started ({time.strftime('%Y-%m-%d')}) ===")
    log(f"Building into {work_db}")
    log(f"  -> becomes {db} on success"
        f"{'' if os.path.exists(db) else ' (no previous database present)'}")

    log("--- Phase 1: cleanup ---")
    cleanup_old_data()

    log("--- Phase 2: download ---")
    download_fcc_zip(ZIP_PATH)

    log("--- Phase 3: build database ---")
    build_database(ZIP_PATH, work_db)

    log("--- Phase 4: geocode (Census batch + cache) ---")
    geocode_database(work_db, args.cache_dir, args.workers,
                     args.miss_retry_days)

    log("--- Phase 5: ZIP-centroid fallback ---")
    if args.no_zip_fallback:
        log("skipped (--no-zip-fallback)")
    else:
        apply_zip_fallback(work_db, args.cache_dir, ref_opts)

    log("--- Phase 6: county assignment ---")
    if args.no_county:
        log("skipped (--no-county)")
    else:
        assign_counties(work_db, args.cache_dir, ref_opts)

    log("--- Phase 7: DXCC entity ---")
    if args.no_dxcc:
        log("skipped (--no-dxcc)")
    else:
        assign_dxcc(work_db)

    log("--- Phase 8: continent (NA/OC lookup) ---")
    if args.no_continent:
        log("skipped (--no-continent)")
    elif args.no_dxcc:
        # continent is derived from dxcc_id; without Phase 7 every row is NULL
        log("skipped (--no-dxcc: nothing to derive continent from)")
    else:
        assign_continent(work_db)

    log("--- Phase 9: ARRL section ---")
    if args.no_section:
        log("skipped (--no-section)")
    else:
        assign_sections(work_db)

    log("--- Phase 10: finalize ---")
    finalize(work_db, db)

    log(f"=== SUCCESS: {db} in {(time.time() - t0) / 60:,.1f} minutes ===")
    replay_notices()
    _log_fh.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _INTERRUPTED.set()
        log("Interrupted by user (Ctrl-C). Any completed geocode batches are "
            "cached; rerun the same command to resume. The existing database "
            "is untouched - the .new file is cleaned up by the next run.")
        try:
            if _log_fh:
                _log_fh.flush()
                _log_fh.close()
        except Exception:
            pass
        # A plain sys.exit() would still hang: at interpreter shutdown the
        # ThreadPoolExecutor's atexit hook JOINS every worker thread, and a
        # worker blocked in a slow Census read won't return until its 1800s
        # socket timeout. Progress is already committed to the cache, so
        # terminate immediately instead of waiting on those threads.
        os._exit(130)
