#!/usr/bin/env python3
r"""
importer_ca.py - Canadian amateur callsign importer for lookup_data.sqlite.

Downloads ISED's amateur callsign data and builds the `ca_operators` table of
lookup_data.sqlite, laid out exactly like `fcc_operators` (same columns, order
and types) so both countries can be queried uniformly; fields Canada does not
publish are left NULL. Run via run_importers.py (option 3, which calls run())
or directly for the flags below.

Source: https://ised-isde.canada.ca/site/amateur-radio-operator-certificate-services/en/downloads

Phases, each marked with its skip flag:
   1 cleanup: remove the wreckage of a failed run (caches, zip and published
     table are kept).  2 download + verify amateur_delim.zip, falling back to
     the local copy if ISED is unreachable.  3 build caches/ca_work.sqlite,
     verified against the source line count.  4 geocode streets via geo.ca
     through a persistent cache (--no-geocode).  5 town centroid for what the
     street geocoder missed (--no-geocode).  6 cross-check each coordinate
     against its own postal code, StatCan FSA boundaries (--no-postal).
     7 province interior point, last resort (--no-province).  8 county, by
     point-in-polygon against StatCan census divisions (--no-county).  9 DXCC
     entity, always Canada/1 (--no-dxcc).  10 continent, always NA
     (--no-continent).  11 RAC contest section (--no-section).  12 publish.

Phases 3-12 operate on the work database, never on lookup_data.sqlite; Phase 12
copies the finished table across in one transaction, so until it commits the
published table is the previous run's, intact and queryable.

geocode_match values, finest to coarsest: 'Street', 'Street_Approx',
'FSA_Centroid' (median FSA radius 3.0 km in town, 43 km rural), 'City_Centroid',
'Province_Centroid', NULL. Validation carries the weight here: geo.ca returns 25
fuzzy candidates for any query and has no "not found" signal, so both pickers
validate the province, and city lookups also check the name and that the feature
is a populated place rather than a lake or the province itself. Neither can
catch a correct street name in the wrong town - Phase 6's postal code, which
geo.ca never sees, is what catches those. Nothing qualifying leaves the row NULL
and reported in logs/ca_unmatched_addresses.csv rather than confidently wrong.

Every path is fixed under the directory holding this script: lookup_data.sqlite,
downloads/ (the ISED dump and the two StatCan boundary zips), caches/ (the work
database and the geocode cache), logs/. Other flags: --workers N (default 5),
--limit N (geocode only N addresses, testing), --miss-retry-days D (default 30).

Exit status: 0 = success, non-zero = download or verification failure. The
geocode phases are safe to rerun after an interruption; the cache is committed
every CACHE_FLUSH lookups.

Requires Python 3 + `requests`; Phases 6-8 also need `shapely`, `pyshp` and
`pyproj`. Run through the project virtualenv:

    .venv\Scripts\python run_importers.py    (Windows)
    ./.venv/bin/python run_importers.py      (macOS/Linux)
"""

import argparse
import csv
import io
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests

import sections

# --- constants -------------------------------------------------------------- #

CA_URL = "https://apc-cap.ic.gc.ca/datafiles/amateur_delim.zip"
ZIP_NAME = "amateur_delim.zip"
DATA_MEMBER = "amateur_delim.txt"
MAX_RETRIES = 6
RUN_LOG = "ca_run.log"

TABLE = "ca_operators"      # the one table this importer owns in the shared db
WORK_TABLE = "operators"    # its name inside the private work database

# requests' default agent is a common target for blanket bot rules on public
# government endpoints, which surface as a 403 on a URL a browser can fetch.
HTTP_HEADERS = {"User-Agent": "ca-amateur-db/1.0 (+bulk data refresh script)"}

# Natural Resources Canada / geo.ca geolocation service (free, no key).
GEOLOC_URL = "https://www.geolocator.api.geo.ca/geolocation/en/locate"
GEOLOC_TRIES = 4    # attempts per query before calling it a transient failure
CACHE_FLUSH = 100   # commit cache (= resume checkpoint) every N lookups

# How long the main thread may block while waiting for lookups. It has to be
# BOUNDED: see the comment in _run_pool() - an unbounded wait cannot be
# interrupted on Windows, which is the whole reason this constant exists.
POLL_SECONDS = 0.5
# Prefixed: caches/ is flat and shared, and every geocoding importer wants a
# file by this name with an incompatible schema inside it.
CACHE_DB = "ca_geocode_cache.sqlite"

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(HERE, "downloads")
CACHES_DIR = os.path.join(HERE, "caches")
LOGS_DIR = os.path.join(HERE, "logs")
DB_PATH = os.path.join(HERE, "lookup_data.sqlite")
WORK_DB = os.path.join(CACHES_DIR, "ca_work.sqlite")
# Downloaded via ZIP_PATH + ".part" and renamed into place only once verified,
# and never deleted: Phase 2 falls back to it when ISED is unreachable.
ZIP_PATH = os.path.join(DOWNLOADS_DIR, ZIP_NAME)
# Run artifacts, rewritten from scratch every time. Prefixed: logs/ is shared.
UNMATCHED_CSV = os.path.join(LOGS_DIR, "ca_unmatched_addresses.csv")
# The extracted data member is kept only when its header does not match the
# expected schema - the evidence of what ISED changed.
HEADER_MISMATCH_PATH = os.path.join(LOGS_DIR, "ca_header_mismatch.txt")

# The header the data file must start with; a mismatch aborts the build rather
# than silently misreading columns.
EXPECTED_HEADER = (
    "callsign;first_name;surname;address_line;city;prov_cd;postal_code;"
    "qual_a;qual_b;qual_c;qual_d;qual_e;club_name;club_name_2;club_address;"
    "club_city;club_prov_cd;club_postal_code")
CA_FIELDS = EXPECTED_HEADER.split(";")

# Qualification letter -> description (from readme_amat_delim.txt).
QUAL_DESC = {"A": "Basic", "B": "5 WPM", "C": "12 WPM", "D": "Advanced",
             "E": "Basic with Honours"}

# Province code -> the English name geo.ca puts in result titles. Used to reject
# cross-province fuzzy matches.
PROVINCE_NAMES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}

# Feature kind geo.ca prints in a result title's trailing "(...)". A town
# centroid must come from a populated place: for a town it does not know the
# service happily offers a lake (half the Geoname candidates) or the province.
PLACE_KINDS = frozenset("""CITY|TOWN|VILLAGE|HAMLET|MUNICIPALITY|TOWNSHIP|
COMMUNITY|LOCALITY|SETTLEMENT|BOROUGH|PARISH|UNINCORPORATED AREA|
INDIAN RESERVE|INDIAN SETTLEMENT|RURAL COMMUNITY|NORTHERN HAMLET|
NORTHERN VILLAGE|NORTHERN COMMUNITY|RESORT VILLAGE|SUMMER VILLAGE|
DISTRICT MUNICIPALITY|REGIONAL MUNICIPALITY|RURAL MUNICIPALITY|
CHARTERED COMMUNITY""".replace("\n", "").split("|"))
# Never a town centroid however well the name matches - far too coarse.
REJECT_KINDS = frozenset({"PROVINCE", "TERRITORY", "COUNTRY"})

# An address with no civic street gives a street geocoder nothing to match, so
# geo.ca answers with an arbitrary same-province street reported as a confident
# hit. These skip Phase 4 and are placed by the Phase 5 town centroid.
NO_CIVIC_STREET = re.compile(
    r"^\s*(P\.?\s?O\.?\s*BOX|BOX|C\.?P\.?|CASE POSTALE|R\.?\s?R\.?\s*#?\s*\d|"
    r"RURAL ROUTE|ROUTE RURALE|GENERAL DELIVERY|POSTE RESTANTE|GD|SITE|COMP)\b",
    re.I)

# Street-type words and directions, dropped when comparing street names, so
# 'GOWDY STREET' matches 'Gowdy Avenue' and 'Rue Pellan' matches 'Pellan'.
STREET_TYPE_WORDS = frozenset("""
STREET ST STR AVENUE AVE AV ROAD RD DRIVE DR COURT CRT CT CRESCENT CRES CRESC
CR BOULEVARD BLVD BOUL BLV PARKWAY PKY PKWY PWY PLACE PL LANE LN TRAIL TR TRL
HIGHWAY HWY ROUTE RTE TERRACE TERR CIRCLE CIR CIRCUIT SQUARE SQ WAY CLOSE CL
GATE GROVE GRV HEIGHTS HTS GARDENS GARDEN GDNS GDN MEWS RIDGE POINT PT BAY
PARK PK GREEN GRN LINK LOOP ROW RISE VIEW VILLAS COMMON COMMONS LANDING MANOR
HILL HOLLOW PATH WALK WYND CONCESSION CONC SIDEROAD SIDERD LINE RUE CHEMIN CH
MONTEE RANG IMPASSE ALLEE COTE PROMENADE SENTIER CARRE CROISSANT TERRASSE
""".split())
STREET_DIRS = frozenset("""
N S E W NE NW SE SW NORTH SOUTH EAST WEST NORTHEAST NORTHWEST SOUTHEAST
SOUTHWEST NORD SUD EST OUEST""".split())
# Unit / apartment markers: the street name stops here.
STREET_UNIT_WORDS = frozenset("""
APT APP APPT SUITE UNIT BUREAU PH PENTHOUSE FLOOR FL RM ROOM LOT BOX CP PO CO
""".split())
# Trailing rural-route / site / compartment junk: '6210 MAPLE DR RR1 S6 C27'.
STREET_TAIL_JUNK = re.compile(r"^(RR|R|S|C|SITE|COMP|STN|STATION|GD)\d*\Z")
STREET_ABBREV = {"LK": "LAKE", "MT": "MOUNT", "FT": "FORT", "PTE": "POINTE"}
_NUMERIC_TOKEN = re.compile(r"\d+[A-Z]?\Z")
POSTAL_RE = re.compile(r"^[A-Z]\d[A-Z]\d[A-Z]\d$")

# Provinces are not separate DXCC entities, so there is nothing to split on.
DXCC_CANADA = ("Canada", 1)
CONTINENT_CANADA = "NA"

# StatCan 2021 census divisions (~140 MB) and Forward Sortation Areas (~162 MB),
# both NAD83 Statistics Canada Lambert / EPSG:3347. Downloaded once and reused.
STATCAN_CD_URL = (
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
    "boundary-limites/files-fichiers/lcd_000b21a_e.zip")
STATCAN_CD_ZIP = "lcd_000b21a_e.zip"
STATCAN_FSA_URL = (
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
    "boundary-limites/files-fichiers/lfsa000b21a_e.zip")
STATCAN_FSA_ZIP = "lfsa000b21a_e.zip"

# A postal code's first letter encodes the province. Where the two disagree
# (~0.09% of rows) the source contradicts itself and Phase 6 skips the row.
POSTAL_PROVINCE = {
    "A": {"NL"}, "B": {"NS"}, "C": {"PE"}, "E": {"NB"},
    "G": {"QC"}, "H": {"QC"}, "J": {"QC"},
    "K": {"ON"}, "L": {"ON"}, "M": {"ON"}, "N": {"ON"}, "P": {"ON"},
    "R": {"MB"}, "S": {"SK"}, "T": {"AB"}, "V": {"BC"},
    "X": {"NT", "NU"}, "Y": {"YT"},
}
# A coordinate contradicts its postal code once it lies further outside the FSA
# than the FSA's own radius - exactly when the FSA interior point is the closer
# estimate. The floor keeps tight urban FSAs from churning on rounding.
FSA_TOLERANCE_FLOOR_KM = 5.0

# StatCan PRUID -> province code, for the Phase 7 province interior points.
PRUID_TO_PROV = sections.PRUID_TO_PROV

# Province -> RAC section, and the Ontario census-division split. Both live in
# sections.py: importer_boundaries.py tags its census-division polygons with
# the same tables, so a row's section must not depend on which importer
# answered. `county` is stored in the cd_short_name() form those tables expect.
SECTION_BY_PROVINCE = sections.SECTION_BY_PROVINCE
ON_COUNTY_SECTION = sections.ON_SECTION_BY_CD


# --- logging (console + utf-8 log file) ------------------------------------- #

_log_fh = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


# --- field cleanup ---------------------------------------------------------- #

def clean(v):
    """Strip surrounding whitespace; empty string -> None."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def norm_province(v):
    """Upper-case a province code to canonical form (blank/None -> None)."""
    v = clean(v)
    return v.upper() if v else None


def clean_postal(v):
    """Upper-case a postal code and reformat to "A1A 1A1" when it is canonical;
    malformed ones (O/0 typos, wrong lengths) are cleaned but not guessed at."""
    v = clean(v)
    if not v:
        return None
    compact = v.upper().replace(" ", "")
    if POSTAL_RE.match(compact):
        return f"{compact[:3]} {compact[3:]}"
    return v.upper()


def operator_class(quals):
    """(code, description) from the qual_a..qual_e flags, e.g. ("ACD", "Basic;
    12 WPM; Advanced"). (None, None) when no qualification is held."""
    letters = [ltr for ltr, val in zip("ABCDE", quals) if clean(val)]
    if not letters:
        return None, None
    return "".join(letters), "; ".join(QUAL_DESC[ltr] for ltr in letters)


# --- database connections --------------------------------------------------- #
#
# Every connection is registered here so run() can close whatever a phase that
# raised never got to close. run_importers.py returns to its menu rather than
# exiting, so an orphaned handle lives for the rest of the session - and on
# Windows an open handle makes the next run's Phase 1 cleanup of the work
# database fail outright, silently reusing a stale file.

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


def count_rows(db, where="1"):
    """COUNT(*) of work-table rows matching `where`."""
    con = connect(db)
    n = con.execute(f"SELECT COUNT(*) FROM operators WHERE {where}").fetchone()[0]
    con.close()
    return n


def log_breakdown(con, column, none_label, width=26):
    """Log a row count per distinct value of `column`."""
    log(f"{column} breakdown:")
    for value, n in con.execute(
        f"SELECT COALESCE({column}, ?), COUNT(*) FROM operators "
        f"GROUP BY {column} ORDER BY COUNT(*) DESC", (none_label,)
    ):
        log(f"  {value:>{width}}: {n:>9,}")


# --- Phase 1: cleanup ------------------------------------------------------- #

def cleanup_old_data():
    """Delete what a previous run stranded; caches, the zip and the published
    table are deliberately left alone (each is replaced atomically by the phase
    that owns it, so a failed run leaves the previous good copy in place)."""
    removed = 0
    for path in (WORK_DB, WORK_DB + "-journal", ZIP_PATH + ".part",
                 UNMATCHED_CSV, HEADER_MISMATCH_PATH):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                log(f"  could not remove {os.path.basename(path)} ({e})")
                continue
            log(f"  removed {os.path.basename(path)}")
            removed += 1

    # Extraction directories stranded by a run killed between Phases 2 and 3 -
    # the one window where nothing else deletes them.
    for name in os.listdir(CACHES_DIR) if os.path.isdir(CACHES_DIR) else []:
        d = os.path.join(CACHES_DIR, name)
        if not name.startswith("ca_import_") or not os.path.isdir(d):
            continue
        try:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)
        except OSError as e:
            log(f"  could not remove {name} ({e})")
            continue
        log(f"  removed stranded extraction {name}")
        removed += 1

    log(f"Cleanup: {removed} stale file(s) removed; caches preserved. "
        f"The published {TABLE} table and the downloaded zip stay in place "
        f"until each is replaced atomically.")


# --- Phase 2: download + extract + validate --------------------------------- #

def usable_ca_zip(path):
    """True if `path` is a readable zip carrying the ISED data member."""
    try:
        with zipfile.ZipFile(path) as zf:
            return DATA_MEMBER in zf.namelist()
    except Exception:
        return False


def download_ca_zip(dest):
    """Stream amateur_delim.zip from ISED with retries; atomic rename on success.

    The download lands in <dest>.part and is proved openable before it replaces
    the previous copy. If every attempt fails but that copy is intact the run
    continues (loudly) on it; only a total absence of a usable zip exits."""
    tmp = dest + ".part"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"Downloading {CA_URL} (attempt {attempt}) ...")
            with requests.get(CA_URL, stream=True, timeout=(30, 300),
                              headers=HTTP_HEADERS) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        done += len(chunk)
            # A connection cut mid-body just ends iter_content without raising.
            if total and done != total:
                raise RuntimeError(f"truncated: {done:,} of {total:,} bytes")
            if not usable_ca_zip(tmp):
                raise RuntimeError(f"not a readable zip with `{DATA_MEMBER}`")
            os.replace(tmp, dest)
            log(f"Downloaded {os.path.getsize(dest) / 1e6:,.1f} MB -> {dest}")
            return
        except Exception as e:
            if attempt == MAX_RETRIES:
                log(f"  download failed ({e}); no attempts left")
                break
            wait = min(30 * attempt, 180)
            log(f"  download failed ({e}); retrying in {wait}s")
            time.sleep(wait)

    try:
        os.remove(tmp)                 # never leave a partial file behind
    except OSError:
        pass

    if os.path.exists(dest) and usable_ca_zip(dest):
        age_days = (time.time() - os.path.getmtime(dest)) / 86400.0
        log("-" * 70)
        log(f" NOTE: {CA_URL} was unreachable after {MAX_RETRIES} attempts -")
        log(f"   rebuilding from the existing {os.path.basename(dest)} "
            f"({os.path.getsize(dest) / 1e6:,.1f} MB, {age_days:.0f} day(s) old).")
        log("   THE RESULTING DATABASE IS ONLY AS CURRENT AS THAT FILE.")
        log("   Rerun once ISED is reachable again.")
        log("-" * 70)
        return

    sys.exit(f"ERROR: could not download {CA_URL} after {MAX_RETRIES} "
             f"attempts, and no usable local copy exists at {dest}")


def validate_data(data_path):
    """Check the extracted file's header against EXPECTED_HEADER and that it
    holds records. A mismatch means ISED changed the layout, so the file is
    preserved under logs/ before the run aborts."""
    if not os.path.exists(data_path):
        sys.exit(f"ERROR: missing expected data file: {os.path.basename(data_path)}")
    with open(data_path, "r", encoding="utf-8") as fh:
        header = fh.readline().rstrip("\r\n")
        record_count = sum(1 for line in fh if line.strip())
    if header != EXPECTED_HEADER:
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            os.replace(data_path, HEADER_MISMATCH_PATH)
            kept = f"\n  the extracted file is kept at:\n    {HEADER_MISMATCH_PATH}"
        except OSError as e:
            kept = f"\n  (could not preserve the extracted file: {e})"
        sys.exit("ERROR: header does not match expected schema - ISED may have "
                 f"changed the format.\n  expected: {EXPECTED_HEADER}\n"
                 f"  found:    {header}{kept}")
    if record_count == 0:
        sys.exit("ERROR: data file contains a header but no records")
    log(f"  header OK ({len(CA_FIELDS)} fields); {record_count:,} records")


def extract_and_validate(zip_path):
    """Extract the data member to a temporary file and validate its header.

    Returns its path; the caller deletes it once Phase 3 has read it, rather
    than keep a ~35 MB duplicate of what the zip beside it already holds."""
    log(f"Extracting {os.path.basename(zip_path)} ...")
    tmpdir = tempfile.mkdtemp(prefix="ca_import_", dir=CACHES_DIR)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            sys.exit(f"ERROR: corrupt archive member: {bad}")
        zf.extract(DATA_MEMBER, tmpdir)   # the readme members are not needed
    data_path = os.path.join(tmpdir, DATA_MEMBER)
    try:
        validate_data(data_path)
    except BaseException:
        # Terminal, and the caller's `finally` never runs because it never got
        # the path - so clean up here or the temp directory outlives the run.
        discard_extracted(data_path)
        raise
    return data_path


def discard_extracted(data_path):
    """Delete the temporary extraction and its directory. Never fatal - a header
    mismatch has already moved the file to logs/."""
    if not data_path:
        return
    for p in (data_path, os.path.dirname(data_path)):
        try:
            os.remove(p) if os.path.isfile(p) else os.rmdir(p)
        except OSError:
            pass


# --- Phase 3: build the sqlite (FCC-identical schema) ----------------------- #
#
# One schema, two names, exactly as in importer_fcc.py: Phases 3-11 work on
# `operators` in the work database, Phase 12 creates the same thing as
# `ca_operators` in lookup_data.sqlite. {q} is the schema qualifier: "" for the
# work database, "lookup." for the attached shared one.
#
# Each is one statement, executed individually: executescript() would COMMIT any
# open transaction first, and Phase 12 needs its drop/create/copy/index to be a
# single unit.

DROP_TABLE = "DROP TABLE IF EXISTS {q}{table}"

# unique_system_identifier is synthetic - Canada publishes no id, the callsign
# is the real key. Columns ISED does not publish are created but left NULL.
# arrl_section is the Canada-only addition; columns 1-38 stay FCC-identical.
SCHEMA = """
CREATE TABLE {q}{table} (
    unique_system_identifier INTEGER PRIMARY KEY,
    callsign TEXT, entity_name TEXT, first_name TEXT, middle_initial TEXT,
    last_name TEXT, name_suffix TEXT, street_address TEXT, city TEXT,
    state TEXT, zip_code TEXT, po_box TEXT, attention_line TEXT, frn TEXT,
    applicant_type_code TEXT, applicant_type TEXT, radio_service_code TEXT,
    radio_service TEXT, grant_date TEXT, expired_date TEXT, convicted TEXT,
    operator_class TEXT, operator_class_desc TEXT, group_code TEXT,
    region_code TEXT, trustee_callsign TEXT, trustee_indicator TEXT,
    vanity_call_sign_change TEXT, previous_callsign TEXT,
    previous_operator_class TEXT, trustee_name TEXT, coordinates TEXT,
    gridsquare TEXT, geocode_match TEXT, county TEXT, dxcc_entity TEXT,
    dxcc_id INTEGER, continent TEXT, arrl_section TEXT
);
"""

# Canada has no per-county UPDATE phase of the kind that earns fcc_operators its
# second (state, county) index.
INDEXES = (
    "CREATE UNIQUE INDEX {q}idx_{table}_callsign ON {table}(callsign)",
    # Covering indexes for "every geocoded operator in <section>/<state>", the
    # same pair fcc_operators carries so both tables answer the query alike.
    "CREATE INDEX {q}idx_{table}_section_coords"
    " ON {table}(arrl_section, coordinates)",
    "CREATE INDEX {q}idx_{table}_state_coords ON {table}(state, coordinates)",
)

# The coordinates indexes are pointless before the geocoding phases have filled
# the column, and would only slow those UPDATEs down; build them at publish.
BUILD_INDEXES = INDEXES[:1]

# Every schema column except arrl_section, which Phase 11 fills.
INSERT_COLS = [c for c in re.findall(r"(\w+) (?:TEXT|INTEGER)", SCHEMA)
               if c != "arrl_section"]


def _row_to_record(usi, f):
    """Map one parsed Canadian record (18 cleaned fields) onto the FCC schema."""
    (callsign, first_name, surname, address_line, city, prov_cd, postal_code,
     qa, qb, qc, qd, qe, club_name, club_name_2, club_address, club_city,
     club_prov_cd, club_postal_code) = f

    op_class, op_desc = operator_class((qa, qb, qc, qd, qe))
    club = " ".join(p for p in (club_name, club_name_2) if p) or None

    rec = dict.fromkeys(INSERT_COLS)
    if club:
        # Club license: the org is the licensee (its own address preferred),
        # the named person is the sponsor/trustee - as on the FCC side.
        rec.update(
            entity_name=club,
            trustee_name=" ".join(p for p in (first_name, surname) if p) or None,
            street_address=club_address or address_line,
            city=club_city or city,
            state=norm_province(club_prov_cd or prov_cd),
            zip_code=clean_postal(club_postal_code or postal_code),
            applicant_type_code="B", applicant_type="Amateur Club",
        )
    else:
        rec.update(
            entity_name=" ".join(p for p in (first_name, surname) if p) or None,
            first_name=first_name, last_name=surname,
            street_address=address_line, city=city,
            state=norm_province(prov_cd), zip_code=clean_postal(postal_code),
            applicant_type_code="I", applicant_type="Individual",
        )
    rec.update(unique_system_identifier=usi, callsign=callsign,
               operator_class=op_class, operator_class_desc=op_desc)
    return rec


def build_database(data_path, db_path):
    """Parse the extracted data into the work database; abort on any mismatch."""
    t0 = time.time()
    log(f"Building {os.path.basename(db_path)} from {os.path.basename(data_path)}")
    if os.path.exists(db_path):
        os.remove(db_path)
    con = connect(db_path)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    con.execute(DROP_TABLE.format(q="", table=WORK_TABLE))
    con.execute(SCHEMA.format(q="", table=WORK_TABLE))

    ins = (f"INSERT INTO operators ({','.join(INSERT_COLS)}) "
           f"VALUES ({','.join('?' * len(INSERT_COLS))})")
    data_lines = 0        # non-header physical lines in the file
    batch, usi = [], 0
    with open(data_path, "r", encoding="utf-8", newline="") as fh:
        fh.readline()     # header, already validated
        for line in fh:
            if not line.strip():
                continue  # tolerate a trailing blank line
            data_lines += 1
            parts = [clean(p) for p in line.rstrip("\r\n").split(";")]
            if len(parts) != len(CA_FIELDS):
                sys.exit(f"ERROR: line {data_lines + 1} has {len(parts)} fields, "
                         f"expected {len(CA_FIELDS)}")
            usi += 1
            rec = _row_to_record(usi, parts)
            batch.append(tuple(rec[c] for c in INSERT_COLS))
            if len(batch) >= 50000:
                con.executemany(ins, batch)
                batch = []
    con.executemany(ins, batch)

    log("Creating indexes ...")
    for stmt in BUILD_INDEXES:
        con.execute(stmt.format(q="", table=WORK_TABLE))
    con.commit()

    log("--- Build verification ---")
    n_ops, dup_cs, n_club, n_class = (
        con.execute(q).fetchone()[0] for q in (
            "SELECT COUNT(*) FROM operators",
            "SELECT COUNT(*) FROM (SELECT callsign FROM operators "
            "GROUP BY callsign HAVING COUNT(*) > 1)",
            "SELECT COUNT(*) FROM operators WHERE applicant_type_code='B'",
            "SELECT COUNT(*) FROM operators WHERE operator_class IS NOT NULL"))
    ok = n_ops == data_lines and dup_cs == 0
    log(f"  data lines {data_lines:>8,}  rows stored {n_ops:>8,}  "
        f"duplicated callsigns {dup_cs}  {'OK' if ok else 'MISMATCH'}")
    log(f"  individuals {n_ops - n_club:>7,}  clubs {n_club:>6,}  "
        f"with qualification {n_class:,}")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()

    if not ok:
        sys.exit(f"ERROR: build verification FAILED for {db_path} - aborting "
                 f"before geocoding. The failed build is left on disk for "
                 f"inspection and is NOT promoted; any previous database is "
                 f"untouched.")
    log(f"Build OK: {os.path.getsize(db_path) / 1e6:,.1f} MB "
        f"in {time.time() - t0:,.0f}s")


# --- Phases 4-5: geocode via geo.ca, through a persistent cache -------------- #

def norm(s):
    """Normalize an address component for use as a cache key."""
    return (s or "").strip().upper()


def orig(s):
    """Trimmed original-case component (empty -> '')."""
    return (s or "").strip()


def strip_accents(s):
    """Remove diacritics: 'Trois-Rivières' -> 'Trois-Rivieres'."""
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def city_key(city, state):
    """Accent-insensitive cache key for a town, so the source's several
    spellings of one place share a single lookup."""
    return ("", strip_accents(norm(city)), norm(state))


def place_key(s):
    """Canonical form for comparing an ISED place name with a geo.ca result
    title. Collapses case, diacritics, punctuation, hyphen-vs-space and the
    Saint/Sainte abbreviations, so 'ST JOHNS' == "St. John's"."""
    s = strip_accents(s).upper().replace("’", "'").replace("`", "'")
    s = re.sub(r"[^A-Z0-9]+", " ", re.sub(r"[.']", "", s))
    s = re.sub(r"\bSAINTE\b", "STE", s)
    return re.sub(r"\s+", " ", re.sub(r"\bSAINT\b", "ST", s)).strip()


def maidenhead(lat, lon):
    """4-character Maidenhead locator from decimal-degree lat/lon.

    Four, not six, to match importer_fcc.py's maidenhead4(): `gridsquare` is one
    of the columns the two operator tables share, so a 6-character value here
    would make LENGTH(gridsquare) - and any prefix join across the tables - mean
    something different on each side. Four characters is also all the coarser
    placements (FSA, city and province centroids) can honestly support.
    """
    lon += 180.0
    lat += 90.0
    if not (0 <= lon < 360 and 0 <= lat < 180):
        return None
    return (chr(ord("A") + int(lon // 20)) + chr(ord("A") + int(lat // 10))
            + str(int((lon % 20) // 2)) + str(int((lat % 10) // 1)))


_thread_local = threading.local()

# Set on Ctrl-C. Worker threads watch it so an interrupt stops them promptly
# instead of waiting out their retry backoff (the signal reaches only the main
# thread).
_INTERRUPTED = threading.Event()

# query_fn result meaning "geo.ca never answered". Distinct from a (lat, lon,
# label) hit and from None (a real no-match): a _TRANSIENT result is not cached,
# so the address is retried next run instead of tombstoned for --miss-retry-days.
_TRANSIENT = object()


def _session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "ca-callsign-geocoder/1.0"
        _thread_local.session = s
    return s


def geoloc(query):
    """Candidate list on an HTTP-200 answer, or None when none was obtained.

    A 200 always carries 25 fuzzy candidates - even for nonsense - so a list is
    authoritative: filters rejecting all 25 is a real no-match, safe to record.
    A 500 (geo.ca's only error, ~half of requests, unrelated to the address)
    says nothing, so None means 'never heard back' and must NOT become a miss.
    """
    for attempt in range(GEOLOC_TRIES):
        if _INTERRUPTED.is_set():
            return None
        try:
            r = _session().get(GEOLOC_URL, params={"q": query}, timeout=(15, 60))
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        if _INTERRUPTED.wait(min(0.4 * (attempt + 1), 4.0)):
            return None
    return None


def _title_province(title):
    """Province name from a result title: the last comma-segment, minus any
    trailing '(Kind)'."""
    return re.sub(r"\s*\(.*\)\s*$", "", (title or "").rsplit(",", 1)[-1]).strip()


def _title_kind(title):
    """Feature kind from a result title: the trailing parenthetical, or '' when
    it carries none (streets and intersections do not)."""
    m = re.search(r"\(([^)]*)\)\s*\Z", title or "")
    return m.group(1).strip().upper() if m else ""


def _coords(res):
    """(lat, lon) from a result's geometry; the service is longitude-first."""
    c = (res.get("geometry") or {}).get("coordinates")
    return (float(c[1]), float(c[0])) if c and len(c) == 2 else None


def street_variants(s):
    """Every plausible comparable form of a street name; two names match when
    their variant sets intersect.

    A single canonical form cannot work: the same street arrives with a civic
    number on one side and without it on the other ('14088 66A Ave' queried,
    '66a Avenue' returned), and prairie-grid streets are named by number
    ('4804 - 49 STREET'), so no fixed rule for dropping leading numbers is safe.
    """
    s = re.sub(r"[.'’`]", "", strip_accents(s or "").upper())
    toks = [t for t in re.sub(r"[^A-Z0-9]+", " ", s).split() if t]
    # ordinals -> bare number: 5TH -> 5, 62ND -> 62, 129E -> 129
    toks = [STREET_ABBREV.get(t, t) for t in
            (re.sub(r"^(\d+)(ST|ND|RD|TH|E|ER|RE)$", r"\1", t) for t in toks)]
    # drop a unit marker plus the number that follows it, keep the rest
    out, skip = [], False
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        # STE is 'Suite' before a number, but 'Sainte' inside a street name
        if t in STREET_UNIT_WORDS or (t == "STE" and _NUMERIC_TOKEN.match(nxt)):
            skip = True
            continue
        if skip and _NUMERIC_TOKEN.match(t):
            skip = False
            continue
        skip = False
        out.append(t)
    toks = out
    while toks and STREET_TAIL_JUNK.match(toks[-1]):
        toks.pop()
    # ST is ambiguous (Street / Saint), so emit both readings.
    saint = [re.sub(r"\A(SAINTE|STE)\Z", "STE", re.sub(r"\A(SAINT|ST)\Z", "ST", t))
             for t in toks]
    variants = set()
    for base in {tuple(toks), tuple(saint)}:
        for drop in (0, 1, 2):
            cur = list(base)
            for _ in range(drop):
                if not (cur and _NUMERIC_TOKEN.match(cur[0])):
                    break
                cur.pop(0)
            for strip_types in (True, False):
                v = [t for t in cur if not (strip_types and (
                    t in STREET_TYPE_WORDS or t in STREET_DIRS))]
                while len(v) > 1 and _NUMERIC_TOKEN.match(v[-1]):
                    v.pop()
                key = "".join(t[:-1] if len(t) > 4 and t.endswith("S") else t
                              for t in v)
                if key:
                    variants.add(key)
    return variants


def pick_street(results, want_prov, want_street):
    """First Street/Address result in the wanted province whose street name
    matches the one queried -> (lat, lon, 'Street' | 'Street_Approx').

    The name check matters because geo.ca answers an address it cannot resolve
    with an unrelated same-province street. The result's CITY is deliberately
    not checked - geo.ca reports boroughs and amalgamated municipalities under
    the absorbing city (Scarborough -> City Of Toronto), which would reject
    thousands of correct matches. `want_prov` None skips the province check.
    """
    want = street_variants(want_street)
    if not want:
        return None
    for res in results or []:
        t = res.get("type", "")
        if not (t.endswith("Street") or t.endswith("Address")):
            continue
        title = res.get("title", "")
        if want_prov and _title_province(title).casefold() != want_prov.casefold():
            continue
        if not (street_variants(title.split(",")[0]) & want):
            continue
        latlon = _coords(res)
        if latlon:
            return (*latlon, "Street"
                    if res.get("qualifier") == "INTERPOLATED_POSITION"
                    else "Street_Approx")
    return None


def pick_city(results, want_prov, want_city):
    """The Geoname that IS `want_city`, in `want_prov` -> (lat, lon,
    'City_Centroid'), else None.

    The province check alone is nowhere near enough: a town geo.ca does not know
    matches whatever same-province noise ranks first, usually a lake. So a
    candidate must also name the town asked for and be a populated place, and a
    populated kind wins over a physical feature of the same name.
    """
    want_key = place_key(want_city)
    if not want_key:
        return None      # city is '-' / '.' / blank: nothing to validate against
    fallback = None
    for res in results or []:
        title = res.get("title", "")
        kind = _title_kind(title)
        if not res.get("type", "").endswith("Geoname") or kind in REJECT_KINDS:
            continue
        if want_prov and _title_province(title).casefold() != want_prov.casefold():
            continue
        if place_key(title.split(",")[0].strip()) != want_key:
            continue
        latlon = _coords(res)
        if not latlon:
            continue
        if kind in PLACE_KINDS:
            return (*latlon, "City_Centroid")
        if fallback is None:
            fallback = (*latlon, "City_Centroid")
    return fallback


def street_query(key, q):
    """key = (STREET, CITY, STATE) - see extract_distinct_streets()."""
    res = geoloc(q)
    return _TRANSIENT if res is None else \
        pick_street(res, PROVINCE_NAMES.get(key[2]), key[0])


def city_query(key, q):
    """key = ('', ACCENT-STRIPPED CITY, STATE) - see city_key()."""
    res = geoloc(q)
    return _TRANSIENT if res is None else \
        pick_city(res, PROVINCE_NAMES.get(key[2]), key[1])


def open_cache():
    """Open (creating if needed) the persistent content-addressed cache."""
    con = connect(os.path.join(CACHES_DIR, CACHE_DB))
    con.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            qkind      TEXT NOT NULL,          -- 'street' or 'city'
            street     TEXT NOT NULL,
            city       TEXT NOT NULL,
            state      TEXT NOT NULL,
            lat        REAL,
            lon        REAL,
            quality    TEXT,
            matched    INTEGER NOT NULL,       -- 1 = hit, 0 = miss
            fetched_at REAL NOT NULL,
            PRIMARY KEY (qkind, street, city, state)
        )""")
    con.commit()
    return con


def load_cache(con, qkind):
    """{(street, city, state): (matched, lat, lon, quality, fetched_at)}."""
    return {(street, city, state): (matched, lat, lon, quality, fetched_at)
            for street, city, state, lat, lon, quality, matched, fetched_at
            in con.execute(
                "SELECT street, city, state, lat, lon, quality, matched, "
                "fetched_at FROM geocode_cache WHERE qkind=?", (qkind,))}


def upsert_cache(con, qkind, rows):
    """rows: (street, city, state, lat, lon, quality, matched, fetched_at)."""
    con.executemany("""
        INSERT INTO geocode_cache
            (qkind, street, city, state, lat, lon, quality, matched, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(qkind, street, city, state) DO UPDATE SET
            lat=excluded.lat, lon=excluded.lon, quality=excluded.quality,
            matched=excluded.matched, fetched_at=excluded.fetched_at
        """, [(qkind, *r) for r in rows])
    con.commit()


def _select_todo(cache, items, retry_before):
    """Split (key, query) items into (todo, hits, fresh_miss) using the cache."""
    todo, hits, fresh_miss = [], 0, 0
    for key, query in items:
        ent = cache.get(key)
        if ent is None or (not ent[0] and ent[4] < retry_before):
            todo.append((key, query))
        elif ent[0]:
            hits += 1
        else:
            fresh_miss += 1
    return todo, hits, fresh_miss


def _run_pool(con, qkind, todo, query_fn, workers, now):
    """Look up each (key, query) concurrently, committing in chunks so an
    interrupted run resumes.

    query_fn returns a (lat, lon, label) hit (cached matched=1), None for a real
    no-match (cached matched=0), or _TRANSIENT, which is not cached at all. On
    Ctrl-C pending lookups are cancelled, in-flight ones told to stop, progress
    flushed, and the interrupt re-raised.
    """
    log(f"  {len(todo):,} {qkind} lookup(s) with {workers} workers ...")

    def work(item):
        if _INTERRUPTED.is_set():
            return None, None               # skip: neither a hit nor a miss
        key, query = item
        return key, query_fn(key, query)

    done = transient = 0
    pending = []
    pool = ThreadPoolExecutor(max_workers=workers)
    unfinished = {pool.submit(work, it) for it in todo}
    try:
        # Bounded waits, NOT as_completed(): a Ctrl-C only becomes a
        # KeyboardInterrupt when the MAIN thread reaches a bytecode boundary,
        # and an unbounded as_completed() gives it none until every lookup has
        # resolved. On Windows that is fatal to the handler below - the wait is
        # a WaitForSingleObject that never looks at the pending signal, so a
        # Ctrl-C sits queued behind the whole phase. Returning every
        # POLL_SECONDS is what makes the interrupt land while there is still
        # something to cancel.
        while unfinished:
            just_done, unfinished = wait(unfinished, timeout=POLL_SECONDS,
                                         return_when=FIRST_COMPLETED)
            for fut in just_done:
                try:
                    key, hit = fut.result()
                except Exception:
                    continue                # worker crashed -> retry next run
                if key is None:             # interrupted worker
                    continue
                if hit is _TRANSIENT:
                    transient += 1
                    continue
                lat, lon, label = hit if hit else (None, None, None)
                pending.append((*key, lat, lon, label, 1 if hit else 0, now))
                done += 1
                if len(pending) >= CACHE_FLUSH:
                    upsert_cache(con, qkind, pending)
                    pending = []
                    log(f"    {done:,}/{len(todo):,} looked up")
    except KeyboardInterrupt:
        _INTERRUPTED.set()
        log(f"  Ctrl-C: stopping {qkind} lookups and saving progress "
            f"({done:,} done this run) ...")
        if pending:
            upsert_cache(con, qkind, pending)
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    if pending:
        upsert_cache(con, qkind, pending)
    log(f"  {qkind}: {done:,} cached this run"
        + (f", {transient:,} transient failure(s) not cached (retry next run)"
           if transient else ""))
    # A high transient rate means geo.ca was degraded for this run, not that
    # these addresses are unresolvable.
    if todo and transient / len(todo) > 0.5:
        log(f"  WARNING: {transient / len(todo):.0%} of {qkind} lookups never "
            f"got an answer - geo.ca looks degraded; rerun to fill them in.")


def extract_distinct_streets(db, limit=None):
    """[(key, query)] for distinct street addresses.

    key is (STREET, CITY, STATE) upper-cased; query keeps the source's original
    casing, which geo.ca resolves far more reliably than ALL-CAPS. Addresses
    with no civic street are skipped and fall through to the Phase 5 town
    centroid, the best that can honestly be said about a PO box.
    """
    con = connect(db)
    rows = con.execute(
        "SELECT street_address, city, state FROM operators "
        "WHERE street_address IS NOT NULL AND TRIM(street_address) <> ''"
    ).fetchall()
    con.close()
    seen, skipped = {}, 0
    for a, b, d in rows:
        key = (norm(a), norm(b), norm(d))
        if key in seen:
            continue
        if NO_CIVIC_STREET.match(key[0]):
            skipped += 1
            continue
        seen[key] = ", ".join(p for p in (orig(a), orig(b), orig(d)) if p)
    if skipped:
        log(f"  {skipped:,} PO-box / rural-route address(es) skipped "
            f"(no civic street; Phase 5 will place them)")
    items = list(seen.items())
    return items[:limit] if limit else items


def extract_distinct_cities(db):
    """[(key, query)] for the towns of rows still lacking coordinates.

    Among the source's spellings of a town we query the one with the MOST
    accented characters, because geo.ca's place search needs the diacritics
    ('Trois-Rivières, QC' resolves; 'TROIS-RIVIERES, QC' returns fuzzy garbage).
    That spelling is chosen from every row of the town, not just the unplaced
    ones - the accented sibling may sit on a row already placed at street level.
    """
    con = connect(db)
    every = con.execute(
        "SELECT city, state FROM operators "
        "WHERE city IS NOT NULL AND TRIM(city) <> ''").fetchall()
    needed = con.execute(
        "SELECT DISTINCT city, state FROM operators WHERE coordinates IS NULL "
        "AND city IS NOT NULL AND TRIM(city) <> ''").fetchall()
    con.close()

    def accents(s):
        return sum(1 for c in s if ord(c) > 127)

    best = {}
    for b, d in every:
        key = city_key(b, d)
        cand = ", ".join(p for p in (orig(b), orig(d)) if p)
        if key not in best or accents(cand) > accents(best[key]):
            best[key] = cand
    want = {city_key(b, d) for b, d in needed}
    return [(k, q) for k, q in best.items() if k in want]


def _write_coords(db, geo, where, key_of):
    """UPDATE coordinates/gridsquare/geocode_match for rows whose normalized
    address is in `geo` ({key: (lat, lon, quality)}). Returns rows updated."""
    con = connect(db)
    updates = []
    for row in con.execute("SELECT unique_system_identifier, street_address, "
                           f"city, state FROM operators WHERE {where}"):
        hit = geo.get(key_of(row))
        if hit:
            lat, lon, quality = hit
            updates.append((f"{lat:.6f},{lon:.6f}", maidenhead(lat, lon),
                            quality, row[0]))
    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?", updates)
    con.commit()
    con.close()
    return len(updates)


def geocode(db, qkind, items, query_fn, where, key_of, workers, miss_retry_days):
    """Look `items` up through the cache, then write what they resolved to.

    Shared by Phases 4 and 5: `qkind` picks the cache partition, `where` and
    `key_of` say which rows the results apply to. Returns rows updated."""
    con_cache = open_cache()
    now = time.time()
    cache = load_cache(con_cache, qkind)
    todo, hits, fresh_miss = _select_todo(cache, items,
                                          now - miss_retry_days * 86400.0)
    log(f"cache: {hits:,} matched reused, {fresh_miss:,} recent misses skipped, "
        f"{len(todo):,} to look up")
    if todo:
        _run_pool(con_cache, qkind, todo, query_fn, workers, now)
        cache = load_cache(con_cache, qkind)
    con_cache.close()
    geo = {k: (v[1], v[2], v[3]) for k, v in cache.items() if v[0]}
    return _write_coords(db, geo, where, key_of)


def geocode_streets(db, workers, limit, miss_retry_days):
    """Phase 4: street-level geocode + write coordinates/gridsquare."""
    items = extract_distinct_streets(db, limit)
    log(f"{len(items):,} distinct street addresses")
    n = geocode(db, "street", items, street_query,
                "street_address IS NOT NULL AND TRIM(street_address) <> ''",
                lambda r: (norm(r[1]), norm(r[2]), norm(r[3])),
                workers, miss_retry_days)
    log(f"{n:,}/{count_rows(db):,} rows have street-level coordinates.")


def geocode_cities(db, workers, miss_retry_days):
    """Phase 5: town-centroid fallback for rows still without coordinates."""
    items = extract_distinct_cities(db)
    log(f"{len(items):,} distinct city/province pairs need a fallback")
    n = geocode(db, "city", items, city_query,
                "coordinates IS NULL AND city IS NOT NULL AND TRIM(city) <> ''",
                lambda r: city_key(r[2], r[3]), workers, miss_retry_days)
    log(f"City fallback: {n:,} rows placed; "
        f"{count_rows(db, 'coordinates IS NULL'):,} rows remain without "
        f"coordinates.")


# --- boundary files (Phases 6-8) -------------------------------------------- #

def fetch_boundary_file(url, zpath, label):
    """Download a StatCan boundary zip once and reuse it thereafter.

    Verified before it is kept: StatCan serves a missing file as an HTML error
    page under HTTP 200, which raise_for_status() cannot see, so a retired URL
    would otherwise leave a 4 KB HTML file named .zip for every later run to
    'reuse' and fail on somewhere far less obvious.
    """
    if os.path.exists(zpath):
        log(f"Using existing {label}: {zpath}")
        return zpath
    log(f"Downloading {label} from {url}")
    tmp = zpath + ".part"
    try:
        with requests.get(url, timeout=(30, 900), stream=True,
                          headers=HTTP_HEADERS) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        if not zipfile.is_zipfile(tmp):
            with open(tmp, "rb") as f:
                head = f.read(120).decode("utf-8", "replace")
            sys.exit(f"ERROR: {url} did not return a zip file "
                     f"({os.path.getsize(tmp):,} bytes). StatCan serves missing "
                     f"files as HTTP 200 HTML, so this usually means the URL has "
                     f"moved - check the boundary-file page for the current "
                     f"release.\n  starts with: {head!r}")
        os.replace(tmp, zpath)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    log(f"  {os.path.getsize(zpath) / 1e6:,.1f} MB -> {zpath}")
    return zpath


def _read_shapefile(zpath):
    """Yield (geometry, record dict) from the .shp/.shx/.dbf inside a zip, in
    the file's native CRS (EPSG:3347).

    The member stem comes from the archive, not from the zip's filename, so a
    StatCan rename inside the archive needs no change here."""
    import shapefile as pyshp
    from shapely.geometry import shape as shapely_shape

    with zipfile.ZipFile(zpath) as zf:
        shp = next((n for n in zf.namelist() if n.lower().endswith(".shp")), None)
        if shp is None:
            sys.exit(f"ERROR: {os.path.basename(zpath)} contains no .shp member "
                     f"(found: {sorted(zf.namelist())[:10]}). Delete it and "
                     f"re-run to refetch, or check the StatCan boundary-file "
                     f"page for the current release.")
        stem = shp[:-4]
        rdr = pyshp.Reader(
            shp=io.BytesIO(zf.read(stem + ".shp")),
            shx=io.BytesIO(zf.read(stem + ".shx")),
            dbf=io.BytesIO(zf.read(stem + ".dbf")),
            encoding="latin-1",     # StatCan dbf holds latin-1 French names
        )
        flds = [f[0] for f in rdr.fields[1:]]
        for sr in rdr.iterShapeRecords():
            yield (shapely_shape(sr.shape.__geo_interface__),
                   dict(zip(flds, sr.record)))


def load_fsa_polygons():
    """{FSA: shapely geometry} in EPSG:3347."""
    zpath = fetch_boundary_file(STATCAN_FSA_URL,
                                os.path.join(DOWNLOADS_DIR, STATCAN_FSA_ZIP),
                                "FSA boundary file")
    out = {}
    for g, d in _read_shapefile(zpath):
        fsa = str(d["CFSAUID"]).strip().upper()
        out[fsa] = g if fsa not in out else out[fsa].union(g)
    log(f"{len(out):,} FSA polygons loaded")
    return out


def cd_short_name(cdname):
    """Short census-division name: drop StatCan's bilingual duplication
    ('Greater Sudbury / Grand Sudbury' -> 'Greater Sudbury') and collapse its
    padding whitespace."""
    return re.sub(r"\s+", " ", (cdname or "").split(" / ")[0]).strip() or None


# Parsed census divisions, shared by Phases 7 and 8 - the same 293 polygons out
# of the same 140 MB file, ~11 s to parse. Reset and released in run().
_cd_features = None


def _read_cd_features():
    """[(geom, pruid, cdname)] for every census division, parsed once."""
    global _cd_features
    if _cd_features is None:
        zpath = fetch_boundary_file(STATCAN_CD_URL,
                                    os.path.join(DOWNLOADS_DIR, STATCAN_CD_ZIP),
                                    "census-division boundary file")
        _cd_features = [(g, str(d["PRUID"]).strip(), cd_short_name(d["CDNAME"]))
                        for g, d in _read_shapefile(zpath)]
        log(f"{len(_cd_features)} census-division polygons loaded (Canada)")
    return _cd_features


def release_cd_features():
    """Drop the parsed census divisions (~176k polygon parts)."""
    global _cd_features
    _cd_features = None


def transformer(src, dst):
    """A lon/lat transform between two EPSG codes."""
    import pyproj
    return pyproj.Transformer.from_crs(src, dst, always_xy=True).transform


# --- Phase 6: postal-code (FSA) cross-check --------------------------------- #

def postal_check(db):
    """Cross-check every coordinate against the row's own postal code, and place
    rows that have a postal code but no coordinate.

    The only geographic evidence here that does not come from geo.ca, which is
    what makes it worth the download: the pickers can only ask whether a
    returned name looks right, so neither catches a correct street name in the
    wrong town. A coordinate is replaced by its FSA's interior point once it
    lies further outside the FSA than the FSA's own radius - the point at which
    the FSA estimate is provably the closer of the two, which also keeps coarse
    rural FSAs (median 43.4 km) from overriding anything better.
    """
    import shapely
    from shapely.strtree import STRtree

    con = connect(db)
    rows = con.execute(
        "SELECT unique_system_identifier, substr(upper(zip_code),1,3), "
        "       coordinates, state, geocode_match FROM operators "
        "WHERE zip_code GLOB '[A-Za-z][0-9][A-Za-z]*'").fetchall()
    if not rows:
        con.close()
        log("Postal check: no usable postal codes.")
        return

    fsa_geom = load_fsa_polygons()
    to3347 = transformer("EPSG:4326", "EPSG:3347")
    to4326 = transformer("EPSG:3347", "EPSG:4326")

    # radius of the equivalent circle, and an interior point, per FSA
    radius_km, interior = {}, {}
    for f, g in fsa_geom.items():
        radius_km[f] = math.sqrt((g.area / 1e6) / math.pi)
        p = g.representative_point()          # guaranteed inside, unlike centroid
        lon, lat = to4326(p.x, p.y)
        interior[f] = (lat, lon)

    usable, skipped_prov, unknown_fsa = [], 0, 0
    for pk, fsa, coords, state, gm in rows:
        if fsa not in fsa_geom:
            unknown_fsa += 1
        elif state and state not in POSTAL_PROVINCE.get(fsa[0], set()):
            skipped_prov += 1          # postal code and province disagree
        else:
            usable.append((pk, fsa, coords, gm))

    placed = [r for r in usable if r[2]]
    missing = [r for r in usable if not r[2]]
    log(f"Postal check: {len(usable):,} rows with a usable FSA "
        f"({len(placed):,} already placed, {len(missing):,} unplaced); "
        f"{skipped_prov:,} skipped (postal/province disagree), "
        f"{unknown_fsa:,} unknown FSA")

    updates, corrected_by_kind = [], {}
    if placed:
        lats = [float(r[2].split(",")[0]) for r in placed]
        lons = [float(r[2].split(",")[1]) for r in placed]
        xs, ys = to3347(lons, lats)
        pts = shapely.points(xs, ys)
        # one vectorised pass to find the points already inside their own FSA
        keys = list(fsa_geom)
        tree = STRtree([fsa_geom[f] for f in keys])
        inside = set()
        for start in range(0, len(pts), 50000):
            pi, gi = tree.query(pts[start:start + 50000], predicate="within")
            for a, b in zip(pi, gi):
                if keys[b] == placed[start + a][1]:
                    inside.add(start + a)
        for i, (pk, fsa, coords, gm) in enumerate(placed):
            if i in inside:
                continue
            dist_km = fsa_geom[fsa].distance(pts[i]) / 1000.0
            if dist_km <= max(FSA_TOLERANCE_FLOOR_KM, radius_km[fsa]):
                continue
            lat, lon = interior[fsa]
            updates.append((f"{lat:.6f},{lon:.6f}", maidenhead(lat, lon),
                            "FSA_Centroid", pk))
            corrected_by_kind[gm] = corrected_by_kind.get(gm, 0) + 1

    for pk, fsa, coords, gm in missing:
        lat, lon = interior[fsa]
        updates.append((f"{lat:.6f},{lon:.6f}", maidenhead(lat, lon),
                        "FSA_Centroid", pk))

    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?", updates)
    con.commit()
    con.close()

    log(f"  corrected {sum(corrected_by_kind.values()):,} coordinate(s) that "
        f"contradicted their postal code:")
    for kind, n in sorted(corrected_by_kind.items(), key=lambda kv: -kv[1]):
        log(f"      was {str(kind):>14}: {n:>7,}")
    log(f"  placed {len(missing):,} previously-unplaced row(s) from their "
        f"postal code")
    total, remaining = count_rows(db), count_rows(db, "coordinates IS NULL")
    log(f"  {total - remaining:,}/{total:,} rows now have coordinates "
        f"({remaining:,} still without)")


# --- Phase 7: province-centroid last resort --------------------------------- #

def load_province_points():
    """{province code: (lat, lon)} - an interior point of each province, taken
    from its LARGEST census division rather than from the whole province
    unioned: both mean 'somewhere inside the province', but unary_union is
    effectively quadratic in part count and cost 132 s of the phase's 143 s."""
    by_prov = {}
    for geom, pruid, _cdname in _read_cd_features():
        prov = PRUID_TO_PROV.get(pruid)
        if prov:
            by_prov.setdefault(prov, []).append(geom)

    to4326 = transformer("EPSG:3347", "EPSG:4326")
    points = {}
    for prov, geoms in by_prov.items():
        # Largest by area, so the choice does not depend on shapefile order.
        p = max(geoms, key=lambda g: g.area).representative_point()
        lon, lat = to4326(p.x, p.y)
        points[prov] = (lat, lon)
    log(f"{len(points)} province interior point(s) derived")
    return points


def assign_province_fallback(db):
    """Give any row still without coordinates, but carrying a province, that
    province's interior point ('Province_Centroid').

    Deliberately coarse - it says no more than `state` already does - so these
    rows are excluded from the county assignment, and therefore from the Ontario
    section split, rather than handed a made-up census division.
    """
    con = connect(db)
    todo = con.execute(
        "SELECT unique_system_identifier, TRIM(UPPER(state)) FROM operators "
        "WHERE coordinates IS NULL AND state IS NOT NULL AND TRIM(state) <> ''"
    ).fetchall()
    if not todo:
        con.close()
        log("Province fallback: nothing to place.")
        return

    points = load_province_points()
    updates, unknown = [], 0
    for pk, st in todo:
        pt = points.get(st)
        if not pt:
            unknown += 1
            continue
        updates.append((f"{pt[0]:.6f},{pt[1]:.6f}", maidenhead(*pt),
                        "Province_Centroid", pk))
    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?", updates)
    con.commit()
    con.close()

    log(f"Province fallback: {len(updates):,} row(s) placed at their province's "
        f"interior point (Province_Centroid)"
        + (f"; {unknown:,} had an unrecognised province code" if unknown else ""))
    total, remaining = count_rows(db), count_rows(db, "coordinates IS NULL")
    log(f"  {total - remaining:,}/{total:,} rows now have coordinates "
        f"({remaining:,} still without - no province at all)")


# --- Phase 8: county (census division) assignment --------------------------- #

def assign_counties(db):
    """Fill `county` by point-in-polygon against Canada's census divisions.

    Distinct coordinates are resolved once (many rows share a point); points
    inside no division (rounding / just offshore) snap to the nearest.
    Province_Centroid rows are excluded - their coordinate is a whole-province
    placeholder, so whichever division it lands in would be a wrong county.
    """
    import shapely
    from shapely.strtree import STRtree

    where = ("coordinates IS NOT NULL AND county IS NULL "
             "AND geocode_match IS NOT 'Province_Centroid'")
    con = connect(db)
    coords = [r[0] for r in con.execute(
        f"SELECT DISTINCT coordinates FROM operators WHERE {where}")]
    if not coords:
        con.close()
        log("County: nothing to assign.")
        return
    log(f"Resolving county for {len(coords):,} distinct coordinate(s) ...")

    feats = _read_cd_features()
    cdnames = [f[2] for f in feats]
    tree = STRtree([f[0] for f in feats])
    latlon = [[float(x) for x in c.split(",")] for c in coords]
    xs, ys = transformer("EPSG:4326", "EPSG:3347")([p[1] for p in latlon],
                                                   [p[0] for p in latlon])
    pts = shapely.points(xs, ys)

    county_of = [None] * len(pts)
    for start in range(0, len(pts), 50000):
        pt_idx, poly_idx = tree.query(pts[start:start + 50000], predicate="within")
        for pi, gi in zip(pt_idx, poly_idx):
            county_of[start + pi] = cdnames[gi]   # border ties: last wins
    snapped = 0
    for i, name in enumerate(county_of):
        if name is None:
            county_of[i] = cdnames[tree.nearest(pts[i])]
            snapped += 1
    if snapped:
        log(f"  {snapped} point(s) outside all divisions snapped to nearest")

    coord_county = dict(zip(coords, county_of))
    rows = con.execute("SELECT unique_system_identifier, coordinates "
                       f"FROM operators WHERE {where}").fetchall()
    con.executemany(
        "UPDATE operators SET county=? WHERE unique_system_identifier=?",
        [(coord_county[c], pk) for pk, c in rows if c in coord_county])
    con.commit()
    con.close()
    log(f"County: {count_rows(db, 'county IS NOT NULL'):,}/"
        f"{count_rows(db):,} rows assigned.")


# --- Phases 9-11: DXCC entity, continent, RAC section ----------------------- #

def assign_dxcc(db):
    """Every Canadian amateur license is DXCC entity 'Canada' (1)."""
    con = connect(db)
    con.execute("UPDATE operators SET dxcc_entity=?, dxcc_id=?", DXCC_CANADA)
    con.commit()
    log_breakdown(con, "dxcc_entity", "(undetermined)")
    con.close()


def assign_continent(db):
    """Canada is entirely North America; continent = 'NA' where dxcc_id is set."""
    con = connect(db)
    con.execute("UPDATE operators SET continent=? WHERE dxcc_id IS NOT NULL",
                (CONTINENT_CANADA,))
    con.commit()
    log_breakdown(con, "continent", "(none)", width=6)
    con.close()


def assign_arrl_section(db):
    """Fill `arrl_section`: 1:1 by province except the territories and Ontario,
    whose four sections are looked up from the county Phase 8 resolved. Ontario
    rows with a NULL county (no coordinates, --no-county, or a Province_Centroid
    placement) stay NULL."""
    con = connect(db)
    con.execute("UPDATE operators SET arrl_section = NULL")
    con.executemany("UPDATE operators SET arrl_section=? WHERE state=?",
                    [(sec, pr) for pr, sec in SECTION_BY_PROVINCE.items()])
    con.executemany(
        "UPDATE operators SET arrl_section=? WHERE state='ON' AND county=?",
        [(sec, name) for name, sec in ON_COUNTY_SECTION.items()])
    con.commit()

    on_missing = con.execute(
        "SELECT COUNT(*) FROM operators "
        "WHERE state='ON' AND county IS NOT NULL AND arrl_section IS NULL"
    ).fetchone()[0]
    if on_missing:
        log(f"  WARNING: {on_missing} Ontario row(s) have a county not in "
            f"ON_SECTION_BY_CD (unmapped) - left NULL. Add it in sections.py, "
            f"which importer_boundaries.py reads too.")
    log_breakdown(con, "arrl_section", "(none)", width=8)
    con.close()


# --- Phase 12: publish ------------------------------------------------------ #

def finalize(db, final_db):
    """Write the unmatched report and summary, then publish into `final_db`."""
    con = connect(db)
    rows = con.execute(
        "SELECT callsign, street_address, city, state, zip_code "
        "FROM operators WHERE coordinates IS NULL ORDER BY callsign").fetchall()
    with open(UNMATCHED_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["callsign", "street_address", "city", "state", "zip_code"])
        w.writerows(rows)
    log(f"{len(rows):,} rows without coordinates -> "
        f"{os.path.basename(UNMATCHED_CSV)}")

    log("--- Final summary ---")
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    log(f"  total operators : {total:>9,}")
    located = 0
    for match, n in con.execute(
        "SELECT COALESCE(geocode_match, '(none)'), COUNT(*) FROM operators "
        "GROUP BY geocode_match ORDER BY COUNT(*) DESC"
    ):
        log(f"    {match:>14}: {n:>9,}")
        if match != "(none)":
            located += n
    log(f"    {'located':>14}: {located:>9,} / {total:,} "
        f"({located / total:.2%})" if total else "")

    publish(con, final_db)
    con.close()


def publish(con, final_db):
    """Copy the finished work table into lookup_data.sqlite as TABLE.

    The whole replacement - drop, create, copy, index - is ONE transaction on
    the attached database, so a crash partway through rolls back to the
    previously published table. Only TABLE is touched.
    """
    # Autocommit, so the only transaction is the explicit one below: sqlite3's
    # default mode opens transactions implicitly around DML, which would collide
    # with the BEGIN here (and ATTACH cannot run inside one).
    con.isolation_level = None
    con.execute("ATTACH DATABASE ? AS lookup", (final_db,))
    try:
        replacing = con.execute(
            "SELECT COUNT(*) FROM lookup.sqlite_master "
            "WHERE type='table' AND name=?", (TABLE,)).fetchone()[0] > 0
        log(f"{'Replacing' if replacing else 'Creating'} {TABLE} in "
            f"{os.path.basename(final_db)} ...")
        con.execute("BEGIN IMMEDIATE")
        con.execute(DROP_TABLE.format(q="lookup.", table=TABLE))
        con.execute(SCHEMA.format(q="lookup.", table=TABLE))
        con.execute(f"INSERT INTO lookup.{TABLE} SELECT * FROM main.{WORK_TABLE}")
        for stmt in INDEXES:
            con.execute(stmt.format(q="lookup.", table=TABLE))
        n = con.execute(f"SELECT COUNT(*) FROM lookup.{TABLE}").fetchone()[0]
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("DETACH DATABASE lookup")
        raise
    con.execute("DETACH DATABASE lookup")
    log(f"{'Replaced' if replacing else 'Created'} {TABLE} ({n:,} rows) in "
        f"{final_db}{' (previous version discarded)' if replacing else ''}")


# --- main ------------------------------------------------------------------- #

def preflight(args):
    """Abort before Phase 1 if an enabled phase's packages are missing: Phases
    6-8 import them lazily, hours into a cold run, and discovering one is
    absent there costs the whole download, build and geocode."""
    geo_phases = [p for p, skip in (("6 (postal/FSA)", args.no_postal),
                                    ("7 (province)", args.no_province),
                                    ("8 (county)", args.no_county)) if not skip]
    if not geo_phases:
        return
    # pip name != import name for pyshp.
    missing = [pip for mod, pip in (("shapely", "shapely"), ("shapefile", "pyshp"),
                                    ("pyproj", "pyproj"))
               if not _importable(mod)]
    if missing:
        sys.exit(
            "ERROR: missing required package(s) for the phases you asked for:\n"
            f"  {', '.join(missing)}\n"
            f"  needed by Phase {'; Phase '.join(geo_phases)}\n"
            f"\nInstall them:\n  python -m pip install {' '.join(missing)}\n"
            "  (or: python -m pip install -r requirements.txt)\n"
            "\nOr skip the phases that need them:\n"
            "  python importer_ca.py --no-postal --no-province --no-county\n"
            "  - coordinates still come from Phases 4-5; county, the Ontario\n"
            "    sections, and the FSA cross-check stay NULL.")


def _importable(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def build_parser():
    ap = argparse.ArgumentParser(
        prog="importer_ca.py",
        description="Canadian amateur callsign import: cleanup, download, "
                    "build (FCC-identical schema), geocode, DXCC, continent, "
                    f"then publish as the `{TABLE}` table of lookup_data.sqlite.")
    ap.add_argument("--workers", type=int, default=5,
                    help="concurrent geocode requests (default 5; geo.ca "
                         "throttles per-IP so more workers rarely help)")
    ap.add_argument("--limit", type=int, default=None,
                    help="geocode only the first N distinct addresses (testing)")
    ap.add_argument("--miss-retry-days", type=float, default=30.0,
                    help="re-query a cached miss older than this many days")
    for flag, phase in (("geocode", "Phases 4-5 (coordinates stay NULL)"),
                        ("postal", "Phase 6 (postal-code / FSA cross-check)"),
                        ("province", "Phase 7 (province-centroid last resort)"),
                        ("county", "Phase 8 (county / census division)"),
                        ("dxcc", "Phase 9 (DXCC entity)"),
                        ("continent", "Phase 10 (continent)"),
                        ("section", "Phase 11 (ARRL/RAC section)")):
        ap.add_argument(f"--no-{flag}", action="store_true", help=f"skip {phase}")
    return ap


def run(args=None):
    """Run the whole import. `args` is a parsed Namespace, or None for defaults -
    which is how run_importers.py calls it. Raises SystemExit on failure."""
    global _log_fh

    if args is None:
        args = build_parser().parse_args([])
    preflight(args)

    # Module state is per-process and run_importers.py may call run() twice in
    # one session: reset a log handle left open by a run that died, and an
    # interrupt flag that would make every lookup return immediately.
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None
    _INTERRUPTED.clear()
    _open_cons.clear()
    release_cd_features()

    for d in (DOWNLOADS_DIR, CACHES_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)
    _log_fh = open(os.path.join(LOGS_DIR, RUN_LOG), "w", encoding="utf-8")
    t0 = time.time()
    log(f"=== Canadian amateur import started ({time.strftime('%Y-%m-%d')}) ===")
    log(f"Building into {WORK_DB}")
    log(f"  -> becomes the {TABLE} table of {DB_PATH} on success"
        f"{'' if os.path.exists(DB_PATH) else ' (no lookup database yet; created)'}")

    phases = [
        ("4: geocode street addresses (geo.ca)", args.no_geocode, "--no-geocode",
         lambda: geocode_streets(WORK_DB, args.workers, args.limit,
                                 args.miss_retry_days)),
        ("5: city-centroid fallback", args.no_geocode, "--no-geocode",
         lambda: geocode_cities(WORK_DB, args.workers, args.miss_retry_days)),
        ("6: postal-code (FSA) cross-check", args.no_postal, "--no-postal",
         lambda: postal_check(WORK_DB)),
        ("7: province-centroid last resort", args.no_province, "--no-province",
         lambda: assign_province_fallback(WORK_DB)),
        ("8: county (census division) assignment", args.no_county, "--no-county",
         lambda: assign_counties(WORK_DB)),
        ("9: DXCC entity", args.no_dxcc, "--no-dxcc",
         lambda: assign_dxcc(WORK_DB)),
        ("10: continent", args.no_continent, "--no-continent",
         lambda: assign_continent(WORK_DB)),
        ("11: ARRL/RAC section", args.no_section, "--no-section",
         lambda: assign_arrl_section(WORK_DB)),
        ("12: publish", False, "", lambda: finalize(WORK_DB, DB_PATH)),
    ]

    try:
        log("--- Phase 1: cleanup ---")
        cleanup_old_data()

        log("--- Phase 2: download + extract + validate ---")
        download_ca_zip(ZIP_PATH)
        data_path = extract_and_validate(ZIP_PATH)

        log("--- Phase 3: build database ---")
        try:
            build_database(data_path, WORK_DB)
        finally:
            # A temporary: Phase 3 is its only reader and the zip is still in
            # downloads/, so it goes even when the build fails.
            discard_extracted(data_path)

        for title, skip, flag, action in phases:
            log(f"--- Phase {title} ---")
            if skip:
                log(f"skipped ({flag})")
                continue
            if flag == "--no-section" and args.no_county:
                log("note: Ontario sections need Phase 8 (county); ON stays NULL")
            action()
    finally:
        leaked = close_leaked_connections()
        if leaked:
            log(f"  closed {leaked} database connection(s) left open by a "
                f"phase that failed")
        release_cd_features()

    log(f"=== SUCCESS: {TABLE} in {DB_PATH} "
        f"in {(time.time() - t0) / 60:,.1f} minutes ===")
    _log_fh.close()
    _log_fh = None


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _INTERRUPTED.set()
        log("Interrupted by user (Ctrl-C). Any completed geocode lookups are "
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
        # ThreadPoolExecutor's atexit hook joins every worker, and one blocked in
        # a slow geo.ca read won't return until its ~60s socket timeout.
        # Progress is already committed, so terminate immediately.
        os._exit(130)
