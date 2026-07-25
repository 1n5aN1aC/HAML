#!/usr/bin/env python3
"""
update_ca_db.py - one-shot Canadian amateur callsign database refresh pipeline.

This is the single, self-contained script for the Canadian side: it both
DOWNLOADS ISED's amateur callsign data and BUILDS the database from it (the
former standalone `downloader.py` has been folded in here — there is no longer a
separate download step).

Canadian counterpart to the FCC's update_fcc_db.py. It produces
`ca_amateur.sqlite` with a single `operators` table laid out EXACTLY like the
FCC database (same table, same columns, same types), so both countries can be
queried uniformly. Fields Canada does not publish are simply left NULL.

Source (Government of Canada / ISED):
    https://ised-isde.canada.ca/site/amateur-radio-operator-certificate-services/en/downloads

Phases (mirroring the FCC pipeline; the address-lookup phases are intentionally
DEFERRED for now and only ever leave their columns NULL):

  Phase 1  CLEANUP    - delete what a previous run may have STRANDED (a
                        half-built .new database, a partial .part download).
                        The live database and zip are
                        deliberately NOT deleted - each is replaced by an atomic
                        rename only once its successor is complete, so a run
                        that dies before then leaves the previous good copy
                        exactly where it was. The address-lookup cache
                        (geocode_cache/) is PRESERVED; update_run.log is not
                        deleted here but truncated when the run opens it.
  Phase 2  DOWNLOAD   - fetch a fresh amateur_delim.zip from ISED into
                        amateur_delim.zip.part, verify it opens and carries the
                        data member, and only then rename it over the previous
                        copy. Extract it and validate the header against the
                        known schema. If every attempt fails but the previous
                        copy is intact, the run continues on it (loudly) rather
                        than abandoning everything downstream.
  Phase 3  BUILD      - parse amateur_delim.txt into ca_amateur.sqlite.new: one
                        `operators` row per callsign, Canadian fields mapped
                        onto the FCC schema, qualifications decoded, provinces
                        and postal codes normalized. Verified row-for-row
                        against the source line count.
  Phase 4  GEOCODE    - resolve every distinct (street, city, province) to
                        coordinates via the NRCan / geo.ca geolocation service,
                        through a persistent content-addressed cache so reruns
                        only pay for new/changed addresses. Each matched row
                        gets coordinates + a 6-char Maidenhead gridsquare.
  Phase 5  CITY FALLBK- rows the street geocoder could not place (PO-box /
                        rural-route / no street, or a street miss) get their
                        town's centroid from the same service (only ~4k
                        distinct city/province pairs).
  Phase 6  POSTAL     - cross-check every coordinate against the row's OWN
                        postal code, using StatCan's Forward Sortation Area
                        boundaries. The postal code is independent of geo.ca, so
                        it catches what the geocoder's own filters cannot: a
                        right-street-name-wrong-town match. A coordinate that
                        contradicts its FSA is replaced by the FSA's interior
                        point ('FSA_Centroid'), and rows that never got a
                        coordinate but do have a postal code are placed the same
                        way - so this phase both corrects and extends coverage.
  Phase 7  PROVINCE   - last resort: a row still without coordinates but with a
                        province gets that province's interior point (unioned
                        from the census divisions), labelled 'Province_Centroid'.
                        It says no more than `state` already does, so these rows
                        are excluded from county (and thus the Ontario section
                        split). Only rows with no province at all stay NULL.
  Phase 8  COUNTY     - point-in-polygon of each geocoded row's coordinates
                        against Canada's census divisions (StatCan boundary
                        file); stores the short division name in `county` (the
                        county-equivalent: counties, regional municipalities,
                        districts, numbered 'Division No. N' in the west).
                        Province_Centroid rows are skipped.
  Phase 9  DXCC       - every Canadian amateur license is ARRL DXCC entity
                        "Canada" (1); dxcc_entity / dxcc_id filled accordingly.
  Phase 10 CONTINENT  - Canada is entirely North America; continent = 'NA'.
  Phase 11 SECTION    - RAC section used in ARRL contests, a pure lookup (no
                        geometry). Every province maps 1:1 by `state` except the
                        three territories (shared 'TER') and Ontario, whose four
                        sections (GH/ONE/ONN/ONS) are looked up from the `county`
                        (census division) that Phase 8 resolved. `arrl_section`
                        is a Canada-only column beyond the FCC layout (columns
                        1-38 stay FCC-identical).
  Phase 12 FINALIZE   - VACUUM and print a coverage summary (including the
                        residue that never located); then rename the finished
                        .new database over the previous one. This is the moment
                        the old database is replaced - up to here it is intact
                        and queryable, including while the geocode phases run.

Phases 3-12 all operate on ca_amateur.sqlite.new, never on the live database.
Phase 3 deletes and recreates that working file on every run.

geocode_match values (finest to coarsest): 'Street' (exact civic match),
'Street_Approx' (street centroid), 'FSA_Centroid' (interior point of the postal
code's Forward Sortation Area - median radius 3.0 km in town, 43 km rural),
'City_Centroid' (town centroid
fallback), 'Province_Centroid' (whole-province interior point, last resort),
NULL (no address at all - not even a province). Validation matters more
than it sounds: geo.ca returns 25 fuzzy candidates for ANY query (even nonsense)
and has no "not found" signal, so correctness rests entirely on rejecting the
noise.
  - Both phases are province-validated - the service will happily place a
    street in the wrong province.
  - City lookups are additionally validated on NAME and KIND: the candidate must
    name the town queried (`place_key`) and be a populated place rather than a
    lake, river or the province itself. Province alone left ~39% of town
    centroids wrong, e.g. 'Bristol's Hope, NL' -> Labrador City, 1,133 km away.
  - Neither picker can catch a CORRECT street name in the WRONG town, because
    both only ever ask whether a returned name looks right. Phase 6 catches
    those with the postal code, which is evidence geo.ca never sees.
Where nothing qualifies the row is left NULL and counted in the Phase 12
summary, rather than given a confidently wrong location.

Cleanups applied (parallel to the FCC side):
  - fields stripped of surrounding whitespace; empty -> NULL.
  - province codes upper-cased to canonical form.
  - postal codes upper-cased and normalized to "A1A 1A1" when they match the
    canonical pattern; malformed codes (O/0 typos etc.) are left as cleaned
    text rather than guessed.
  - all schema columns are always created even when 100% empty, so the layout
    is byte-identical to the FCC database.

Usage
-----
    python update_ca_db.py                    # full refresh in script's folder

Every path this pipeline owns is fixed, beside the script:

    ca_amateur.sqlite         the database (built as .new, renamed on success)
    amateur_delim.zip         the ISED dump (downloaded as .part, then renamed)
    amateur_delim.txt         the extracted data file
    update_run.log            this run's log
    geocode_cache/            the persistent cache and the boundary file

There is no flag to relocate the database or to supply your own zip. Both
existed to protect against a Phase 1 that deleted them before the download;
Phase 1 now removes only the wreckage of a failed run, and Phase 2 falls back
to the existing amateur_delim.zip on its own when ISED is unreachable.

    --cache-dir PATH   geocode cache dir (default: geocode_cache here)
    --workers N        concurrent geocode requests (default 5)
    --limit N          geocode only the first N distinct addresses (testing)
    --miss-retry-days D  re-query a cached miss older than D days (default 30)
    --no-geocode       skip Phases 4-5 (leave coordinates NULL)
    --no-postal        skip Phase 6 (postal-code / FSA cross-check + fill)
    --no-province      skip Phase 7 (province-centroid last resort)
    --no-county        skip Phase 8 (county / census-division assignment)
    --no-dxcc          skip Phase 9 (DXCC entity)
    --no-continent     skip Phase 10 (continent)
    --no-section       skip Phase 11 (ARRL/RAC section)

Exit status: 0 = success, non-zero = download or verification failure.
The geocode phase is safe to rerun after an interruption - the cache
preserves every completed lookup (committed every CACHE_FLUSH lookups).

Requires Python 3 + `requests`; Phases 6 (postal), 7 (province) and 8 (county)
additionally need `shapely`, `pyshp`, and `pyproj` for point-in-polygon (skip with
--no-postal / --no-province / --no-county to avoid them). Phase 11 (section) is
pure SQL and needs no extra libs, but its Ontario sections read the county
Phase 8 wrote.
Run through the project virtualenv, which lives one level up in data-parsers/
and is shared with the FCC pipeline:
../.venv/Scripts/python.exe update_ca_db.py  (Windows) /
../.venv/bin/python update_ca_db.py  (macOS/Linux). See ../requirements.txt.
"""

import argparse
import io
import math
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

CA_URL = "https://apc-cap.ic.gc.ca/datafiles/amateur_delim.zip"
ZIP_NAME = "amateur_delim.zip"
DATA_MEMBER = "amateur_delim.txt"
MAX_RETRIES = 6
RUN_LOG = "update_run.log"

# Sent on the bulk downloads (ISED, StatCan). The geocoder has its own agent
# string on its pooled Session (see _session). requests' default
# ("python-requests/2.x") identifies nothing and is a common target for blanket
# bot rules on public government endpoints, which surface as a 403 on a URL
# that works fine in a browser.
HTTP_HEADERS = {"User-Agent": "ca-amateur-db/1.0 (+bulk data refresh script)"}

# Natural Resources Canada / geo.ca geolocation service (free, no key, one
# query at a time). Returns candidates as JSON, longitude-first coordinates.
GEOLOC_URL = "https://www.geolocator.api.geo.ca/geolocation/en/locate"
CACHE_DB = "geocode_cache.sqlite"
CACHE_FLUSH = 100   # commit cache (= resume checkpoint) every N lookups

HERE = os.path.dirname(os.path.abspath(__file__))

# Every file this pipeline owns lives beside the script, under a fixed name.
# These were once --db / --zip, but relocating them only ever created ways for
# the run's own artifacts to drift apart: the report and log stayed here while
# the database moved, so Phase 1 cleaned a path the previous run had not
# written. One directory, one set of names, nothing to keep in sync.
DB_PATH = os.path.join(HERE, "ca_amateur.sqlite")

# Phases 3-12 build here; Phase 12 renames it onto DB_PATH once it is provably
# complete. Until that instant DB_PATH still holds the previous run's database.
WORK_DB = DB_PATH + ".new"

# The ISED dump. Downloaded via ZIP_PATH + ".part" and renamed into place only
# after it is proved openable, so this name never points at a partial file. It
# is deliberately never deleted: when ISED is unreachable, Phase 2 falls back
# to it rather than abandoning the run.
ZIP_PATH = os.path.join(HERE, ZIP_NAME)

# The header row the data file must start with (schema guard: if ISED changes
# the layout, the build aborts rather than silently misreading columns).
CA_FIELDS = [
    "callsign", "first_name", "surname", "address_line", "city", "prov_cd",
    "postal_code", "qual_a", "qual_b", "qual_c", "qual_d", "qual_e",
    "club_name", "club_name_2", "club_address", "club_city", "club_prov_cd",
    "club_postal_code",
]  # 18
EXPECTED_HEADER = ";".join(CA_FIELDS)

# Canadian qualification letter -> description (from readme_amat_delim.txt).
QUAL_DESC = {
    "A": "Basic",
    "B": "5 WPM",
    "C": "12 WPM",
    "D": "Advanced",
    "E": "Basic with Honours",
}

# Canonical province / territory codes (ISED uses these two-letter forms).
CA_PROVINCES = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
})

# Province code -> the English name the geo.ca /en/ service puts in result
# titles. Used to reject cross-province fuzzy matches: geo.ca always returns 25
# fuzzy candidates (even for nonsense) and will confidently place, e.g., a
# Quebec street onto a same-named New Brunswick one, so we only accept a result
# whose province matches the one we queried.
PROVINCE_NAMES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}

# Feature kind geo.ca prints in the trailing "(...)" of a result title. A town
# centroid must come from a POPULATED place: the service happily offers a lake
# (565 of the 1,141 Geoname candidates in a 120-query sample) or the province
# itself for a town it does not know.
PLACE_KINDS = frozenset({
    "CITY", "TOWN", "VILLAGE", "HAMLET", "MUNICIPALITY", "TOWNSHIP",
    "COMMUNITY", "LOCALITY", "SETTLEMENT", "BOROUGH", "PARISH",
    "UNINCORPORATED AREA", "INDIAN RESERVE", "INDIAN SETTLEMENT",
    "RURAL COMMUNITY", "NORTHERN HAMLET", "NORTHERN VILLAGE",
    "NORTHERN COMMUNITY", "RESORT VILLAGE", "SUMMER VILLAGE",
    "DISTRICT MUNICIPALITY", "REGIONAL MUNICIPALITY", "RURAL MUNICIPALITY",
    "CHARTERED COMMUNITY",
})
# Never a town centroid however well the name matches - far too coarse.
REJECT_KINDS = frozenset({"PROVINCE", "TERRITORY", "COUNTRY"})

# --- street-name matching (Phase 4) ---------------------------------------- #
# An address with no civic street - a PO box, rural route or general delivery -
# gives a street geocoder nothing to match, so geo.ca returns an arbitrary
# same-province street and reports it as a confident hit. These skip Phase 4
# entirely and are placed by the Phase 5 town centroid instead.
NO_CIVIC_STREET = re.compile(
    r"^\s*(P\.?\s?O\.?\s*BOX|BOX|C\.?P\.?|CASE POSTALE|R\.?\s?R\.?\s*#?\s*\d|"
    r"RURAL ROUTE|ROUTE RURALE|GENERAL DELIVERY|POSTE RESTANTE|GD|SITE|COMP)\b",
    re.I)

# Street-type words and directions, dropped when comparing street names, so
# 'GOWDY STREET' matches 'Gowdy Avenue' and 'Rue Pellan' matches 'Pellan'.
STREET_TYPE_WORDS = frozenset({
    "STREET", "ST", "STR", "AVENUE", "AVE", "AV", "ROAD", "RD", "DRIVE", "DR",
    "COURT", "CRT", "CT", "CRESCENT", "CRES", "CRESC", "CR", "BOULEVARD",
    "BLVD", "BOUL", "BLV", "PARKWAY", "PKY", "PKWY", "PWY", "PLACE", "PL",
    "LANE", "LN", "TRAIL", "TR", "TRL", "HIGHWAY", "HWY", "ROUTE", "RTE",
    "TERRACE", "TERR", "CIRCLE", "CIR", "CIRCUIT", "SQUARE", "SQ", "WAY",
    "CLOSE", "CL", "GATE", "GROVE", "GRV", "HEIGHTS", "HTS", "GARDENS",
    "GARDEN", "GDNS", "GDN", "MEWS", "RIDGE", "POINT", "PT", "BAY", "PARK",
    "PK", "GREEN", "GRN", "LINK", "LOOP", "ROW", "RISE", "VIEW", "VILLAS",
    "COMMON", "COMMONS", "LANDING", "MANOR", "HILL", "HOLLOW", "PATH", "WALK",
    "WYND", "CONCESSION", "CONC", "SIDEROAD", "SIDERD", "LINE",
    "RUE", "CHEMIN", "CH", "MONTEE", "RANG", "IMPASSE", "ALLEE", "COTE",
    "PROMENADE", "SENTIER", "CARRE", "CROISSANT", "TERRASSE",
})
STREET_DIRS = frozenset({
    "N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST",
    "WEST", "NORTHEAST", "NORTHWEST", "SOUTHEAST", "SOUTHWEST",
    "NORD", "SUD", "EST", "OUEST",
})
# Unit / apartment markers: the street name stops here.
STREET_UNIT_WORDS = frozenset({
    "APT", "APP", "APPT", "SUITE", "UNIT", "BUREAU", "PH", "PENTHOUSE",
    "FLOOR", "FL", "RM", "ROOM", "LOT", "BOX", "CP", "PO", "CO",
})
# Trailing rural-route / site / compartment junk: '6210 MAPLE DR RR1 S6 C27'.
STREET_TAIL_JUNK = re.compile(r"^(RR|R|S|C|SITE|COMP|STN|STATION|GD)\d*\Z")
STREET_ABBREV = {"LK": "LAKE", "MT": "MOUNT", "FT": "FORT", "PTE": "POINTE"}
_NUMERIC_TOKEN = re.compile(r"\d+[A-Z]?\Z")

# Every Canadian amateur license is the single ARRL DXCC entity "Canada" (1),
# which lies entirely in North America. Provinces are NOT separate DXCC
# entities (unlike US territories), so there is nothing to split on.
DXCC_CANADA = ("Canada", 1)
CONTINENT_CANADA = "NA"

# --- ARRL/RAC section (used in ARRL contests) ------------------------------ #
# StatCan 2021 Census Division cartographic boundary file (all Canada, ~140 MB,
# NAD83 Statistics Canada Lambert / EPSG:3347). Downloaded once into cache_dir;
# only the Ontario divisions are used, to split ON into its four sections.
STATCAN_CD_URL = (
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
    "boundary-limites/files-fichiers/lcd_000b21a_e.zip"
)
STATCAN_CD_ZIP = "lcd_000b21a_e.zip"

# StatCan 2021 Forward Sortation Area boundaries (~162 MB, same EPSG:3347).
# An FSA is the first three characters of a postal code. Downloaded once into
# cache_dir alongside the census-division file.
STATCAN_FSA_URL = (
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
    "boundary-limites/files-fichiers/lfsa000b21a_e.zip"
)
STATCAN_FSA_ZIP = "lfsa000b21a_e.zip"

# A postal code's first letter encodes the province, so it can be checked
# against `state` on its own. Where the two disagree (~0.09% of rows) the
# source is self-contradictory and Phase 6 leaves the row alone.
POSTAL_PROVINCE = {
    "A": {"NL"}, "B": {"NS"}, "C": {"PE"}, "E": {"NB"},
    "G": {"QC"}, "H": {"QC"}, "J": {"QC"},
    "K": {"ON"}, "L": {"ON"}, "M": {"ON"}, "N": {"ON"}, "P": {"ON"},
    "R": {"MB"}, "S": {"SK"}, "T": {"AB"}, "V": {"BC"},
    "X": {"NT", "NU"}, "Y": {"YT"},
}

# A coordinate is treated as contradicting its postal code once it lies further
# outside the FSA than the FSA's own radius - i.e. exactly when the FSA's
# interior point is the closer estimate of the truth. The floor keeps boundary
# rounding and the tight urban FSAs (median radius 3.0 km) from churning.
FSA_TOLERANCE_FLOOR_KM = 5.0

# StatCan PRUID (province identifier in the boundary files) -> province code.
# Used to union the census divisions into whole provinces for the last-resort
# province-centroid placement (Phase 7).
PRUID_TO_PROV = {
    "10": "NL", "11": "PE", "12": "NS", "13": "NB", "24": "QC", "35": "ON",
    "46": "MB", "47": "SK", "48": "AB", "59": "BC", "60": "YT", "61": "NT",
    "62": "NU",
}

# Province/territory code -> RAC section. Every province maps 1:1 except the
# three territories (one shared "Territories" section) and Ontario, which is
# NOT here because it is split geographically into four sections (see below).
SECTION_BY_PROVINCE = {
    "NL": "NL", "NS": "NS", "NB": "NB", "PE": "PE", "QC": "QC", "MB": "MB",
    "SK": "SK", "AB": "AB", "BC": "BC",
    "YT": "TER", "NT": "TER", "NU": "TER",   # Yukon + NWT + Nunavut -> TER
}

# Ontario census-division (short name, as stored in `county`) -> RAC section,
# per RAC's official "Ontario Sections effective 01 Jan 2023" list (Golden
# Horseshoe GH now includes Hamilton + Niagara; Kawartha Lakes is ONE).
# Nipissing is split by Algonquin Park (north=ONN, south=ONE); its only
# populated area (North Bay) is in the ONN part, so the whole division is ONN.
# Keyed by county name so Phase 11 is a pure lookup off the county Phase 8 wrote
# (all 49 names are unique within Ontario; verified against the boundary file).
ON_COUNTY_SECTION = {
    # Golden Horseshoe (7)
    "Durham": "GH", "York": "GH", "Toronto": "GH", "Peel": "GH",
    "Halton": "GH", "Hamilton": "GH", "Niagara": "GH",
    # Ontario East (14)
    "Stormont, Dundas and Glengarry": "ONE", "Prescott and Russell": "ONE",
    "Ottawa": "ONE", "Leeds and Grenville": "ONE", "Lanark": "ONE",
    "Frontenac": "ONE", "Lennox and Addington": "ONE", "Hastings": "ONE",
    "Prince Edward": "ONE", "Northumberland": "ONE", "Peterborough": "ONE",
    "Kawartha Lakes": "ONE", "Haliburton": "ONE", "Renfrew": "ONE",
    # Ontario North (10)
    "Nipissing": "ONN", "Manitoulin": "ONN", "Sudbury": "ONN",
    "Greater Sudbury": "ONN", "Timiskaming": "ONN", "Cochrane": "ONN",
    "Algoma": "ONN", "Thunder Bay": "ONN", "Rainy River": "ONN",
    "Kenora": "ONN",
    # Ontario South (18)
    "Dufferin": "ONS", "Wellington": "ONS", "Haldimand-Norfolk": "ONS",
    "Brant": "ONS", "Waterloo": "ONS", "Perth": "ONS", "Oxford": "ONS",
    "Elgin": "ONS", "Chatham-Kent": "ONS", "Essex": "ONS", "Lambton": "ONS",
    "Middlesex": "ONS", "Huron": "ONS", "Bruce": "ONS", "Grey": "ONS",
    "Simcoe": "ONS", "Muskoka": "ONS", "Parry Sound": "ONS",
}

POSTAL_RE = re.compile(r"^[A-Z]\d[A-Z]\d[A-Z]\d$")

# --------------------------------------------------------------------------- #
# Logging (console + utf-8 log file)
# --------------------------------------------------------------------------- #

_log_fh = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


# --------------------------------------------------------------------------- #
# Field cleanup helpers
# --------------------------------------------------------------------------- #

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
    """Normalize a Canadian postal code.

    Upper-case, drop internal spaces, and reformat to "A1A 1A1" when it matches
    the canonical letter-digit pattern. Malformed codes (O/0 typos, wrong
    lengths) are returned cleaned/upper-cased but otherwise untouched rather
    than guessed at.
    """
    v = clean(v)
    if not v:
        return None
    compact = v.upper().replace(" ", "")
    if POSTAL_RE.match(compact):
        return f"{compact[:3]} {compact[3:]}"
    return v.upper()


def operator_class(quals):
    """(code, description) from the five qualification flags.

    `quals` is the list of qual_a..qual_e values. The held letters are
    concatenated ("ACD") for the code and decoded ("Basic; 12 WPM; Advanced")
    for the description. Returns (None, None) when no qualification is held.
    """
    letters = [ltr for ltr, val in zip("ABCDE", quals) if clean(val)]
    if not letters:
        return None, None
    return "".join(letters), "; ".join(QUAL_DESC[ltr] for ltr in letters)


# --------------------------------------------------------------------------- #
# Phase 1 - cleanup
# --------------------------------------------------------------------------- #

def cleanup_old_data():
    """Delete what a previous run stranded. geocode_cache/ is never touched.

    Deliberately does NOT delete the live database or zip. Each is replaced by
    an atomic rename only once its successor is fully built (Phase 12) or fully
    downloaded and verified (Phase 2), so a run that dies anywhere before then
    leaves the previous good copy in place - and Phase 2 can even fall back to
    the old zip when ISED is unreachable. What belongs here is only the wreckage
    of a *failed* run: the half-built work database and a partial download.

    The extracted amateur_delim.txt is not listed: Phase 2 overwrites it from
    the zip on every run, so it always describes the zip currently on disk.
    """
    victims = [
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
# Phase 2 - download + extract + validate
# --------------------------------------------------------------------------- #

def usable_ca_zip(path):
    """True if `path` is a readable zip carrying the ISED data member."""
    if not os.path.exists(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return DATA_MEMBER in zf.namelist()
    except Exception:
        return False


def download_ca_zip(dest):
    """Stream amateur_delim.zip from ISED with retries; atomic rename on success.

    The download lands in <dest>.part and is proved openable (and to carry the
    data member) BEFORE it is renamed over the previous copy, so a failed or
    truncated fetch can never destroy a good one.

    Returns True if a fresh copy was downloaded, False if every attempt failed
    but the previous copy is intact and the run is proceeding on it. Exits only
    when there is no usable zip at all - no later phase means anything without
    one.
    """
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
            # A connection cut mid-body just ends iter_content without raising,
            # so compare against the advertised length before trusting the file.
            if total and done != total:
                raise RuntimeError(f"truncated: {done:,} of {total:,} bytes")
            with zipfile.ZipFile(tmp) as zf:  # sanity: readable zip w/ data
                if DATA_MEMBER not in zf.namelist():
                    raise RuntimeError(f"zip has no `{DATA_MEMBER}`")
            os.replace(tmp, dest)
            log(f"Downloaded {os.path.getsize(dest) / 1e6:,.1f} MB -> {dest}")
            return True
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

    # Phase 1 deliberately left the previous zip in place. If it is intact,
    # rebuilding from it beats aborting: every phase after this one still runs
    # to completion and still verifies - just against older ISED data.
    if usable_ca_zip(dest):
        age_days = (time.time() - os.path.getmtime(dest)) / 86400.0
        log("")
        log("-" * 70)
        log(" NOTE: the ISED download failed - falling back to the local zip")
        log("")
        log(f"   {CA_URL}")
        log(f"   was unreachable after {MAX_RETRIES} attempts.")
        log("")
        log(f"   Rebuilding from the existing {os.path.basename(dest)}"
            f" ({os.path.getsize(dest) / 1e6:,.1f} MB,")
        log(f"   downloaded {age_days:.0f} day(s) ago).")
        log("")
        log("   THE RESULTING DATABASE IS ONLY AS CURRENT AS THAT FILE.")
        log("   Rerun once ISED is reachable again.")
        log("-" * 70)
        log("")
        return False

    sys.exit(f"ERROR: could not download {CA_URL} after {MAX_RETRIES} "
             f"attempts, and no usable local copy exists at {dest}")


def validate_data(data_path):
    """Validate an already-extracted data file: it exists, its header matches the
    known schema, and it holds at least one record (the schema guard folded in
    from the former downloader.py). Returns the record count."""
    if not os.path.exists(data_path):
        sys.exit(f"ERROR: missing expected data file: {os.path.basename(data_path)}")
    with open(data_path, "r", encoding="utf-8") as fh:
        header = fh.readline().rstrip("\r\n")
        record_count = sum(1 for line in fh if line.strip())
    if header != EXPECTED_HEADER:
        sys.exit("ERROR: header does not match expected schema - ISED may have "
                 f"changed the format.\n  expected: {EXPECTED_HEADER}\n"
                 f"  found:    {header}")
    if record_count == 0:
        sys.exit("ERROR: data file contains a header but no records")
    log(f"  header OK ({header.count(';') + 1} fields); {record_count:,} records")
    return record_count


def extract_and_validate(zip_path):
    """Extract the archive here and validate the data header. Returns the path
    to the extracted data file."""
    log(f"Extracting {os.path.basename(zip_path)} ...")
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            sys.exit(f"ERROR: corrupt archive member: {bad}")
        # Only extract the data file we use; the two readme members
        # (readme_amat_delim.txt / lisezmoi_amat_delim.txt) are documentation
        # we do not need on disk.
        zf.extract(DATA_MEMBER, HERE)
    data_path = os.path.join(HERE, DATA_MEMBER)
    validate_data(data_path)
    return data_path


# --------------------------------------------------------------------------- #
# Phase 3 - build the sqlite (FCC-identical schema)
# --------------------------------------------------------------------------- #

# Column layout is byte-identical to fcc_amateur.sqlite so the two databases
# are interchangeable. Columns Canada does not publish are always created but
# left NULL.
SCHEMA = """
DROP TABLE IF EXISTS operators;
CREATE TABLE operators (
    -- key ---------------------------------------------------------------
    unique_system_identifier INTEGER PRIMARY KEY,  -- synthetic row id (Canada
                                                   -- publishes no numeric id;
                                                   -- callsign is the real key)
    callsign                 TEXT,
    -- licensee identity ---------------------------------------------------
    entity_name              TEXT,   -- display name, or club/org name
    first_name               TEXT,   -- given name(s); NULL for club rows
    middle_initial           TEXT,   -- not published by ISED -> NULL
    last_name                TEXT,   -- surname; NULL for club rows
    name_suffix              TEXT,   -- not published -> NULL
    street_address           TEXT,
    city                     TEXT,
    state                    TEXT,   -- province/territory code
    zip_code                 TEXT,   -- postal code
    po_box                   TEXT,   -- not split out by ISED -> NULL
    attention_line           TEXT,   -- not published -> NULL
    frn                      TEXT,   -- FCC-only -> NULL
    applicant_type_code      TEXT,   -- I (individual) / B (club)
    applicant_type           TEXT,   -- decoded
    -- license dates (not published by ISED -> NULL) -----------------------
    radio_service_code       TEXT,
    radio_service            TEXT,
    grant_date               TEXT,
    expired_date             TEXT,
    convicted                TEXT,
    -- amateur-specific ----------------------------------------------------
    operator_class           TEXT,   -- held qualification letters, e.g. "ACD"
    operator_class_desc      TEXT,   -- decoded, e.g. "Basic; 12 WPM; Advanced"
    group_code               TEXT,   -- not published -> NULL
    region_code              TEXT,   -- not published -> NULL
    trustee_callsign         TEXT,   -- not published -> NULL
    trustee_indicator        TEXT,   -- not published -> NULL
    vanity_call_sign_change  TEXT,   -- not published -> NULL
    previous_callsign        TEXT,   -- not published -> NULL
    previous_operator_class  TEXT,   -- not published -> NULL
    trustee_name             TEXT,   -- club sponsor's name (club rows)
    -- geocoding (DEFERRED - address-lookup step not yet built) ------------
    coordinates              TEXT,
    gridsquare               TEXT,
    geocode_match            TEXT,
    county                   TEXT,
    dxcc_entity              TEXT,   -- ARRL DXCC entity name (Phase 9)
    dxcc_id                  INTEGER, -- ARRL DXCC entity number (Phase 9)
    continent                TEXT,   -- 'NA' (Phase 10)
    -- Canada-only addition beyond the FCC layout (columns 1-38 stay identical)
    arrl_section             TEXT    -- RAC/ARRL-contest section (Phase 11)
);
"""

INDEXES = "CREATE UNIQUE INDEX idx_operators_callsign ON operators(callsign);"

# Column order used by the INSERT below (every schema column, once).
INSERT_COLS = [
    "unique_system_identifier", "callsign", "entity_name", "first_name",
    "middle_initial", "last_name", "name_suffix", "street_address", "city",
    "state", "zip_code", "po_box", "attention_line", "frn",
    "applicant_type_code", "applicant_type", "radio_service_code",
    "radio_service", "grant_date", "expired_date", "convicted",
    "operator_class", "operator_class_desc", "group_code", "region_code",
    "trustee_callsign", "trustee_indicator", "vanity_call_sign_change",
    "previous_callsign", "previous_operator_class", "trustee_name",
    "coordinates", "gridsquare", "geocode_match", "county", "dxcc_entity",
    "dxcc_id", "continent",
]


def _row_to_record(usi, f):
    """Map one parsed Canadian record `f` (18 cleaned fields) onto the FCC
    schema, returning a dict of column -> value."""
    (callsign, first_name, surname, address_line, city, prov_cd, postal_code,
     qa, qb, qc, qd, qe, club_name, club_name_2, club_address, club_city,
     club_prov_cd, club_postal_code) = f

    op_class, op_desc = operator_class((qa, qb, qc, qd, qe))

    # Club rows: the org is the licensee (entity_name + its own address); the
    # named person is the sponsor/trustee. Individual rows: person is the
    # licensee. This mirrors how the FCC represents club vs individual licenses.
    club = " ".join(p for p in (club_name, club_name_2) if p) or None
    if club:
        # Club license: the org is the licensee (its own address preferred),
        # the named person is the sponsor/trustee.
        person = " ".join(p for p in (first_name, surname) if p) or None
        rec = dict(
            entity_name=club, first_name=None, last_name=None,
            trustee_name=person,
            street_address=club_address or address_line,
            city=club_city or city,
            state=norm_province(club_prov_cd or prov_cd),
            zip_code=clean_postal(club_postal_code or postal_code),
            applicant_type_code="B", applicant_type="Amateur Club",
        )
    else:
        entity_name = " ".join(p for p in (first_name, surname) if p) or None
        rec = dict(
            entity_name=entity_name, first_name=first_name, last_name=surname,
            trustee_name=None, street_address=address_line, city=city,
            state=norm_province(prov_cd), zip_code=clean_postal(postal_code),
            applicant_type_code="I", applicant_type="Individual",
        )

    return {
        "unique_system_identifier": usi,
        "callsign": callsign,
        "entity_name": rec["entity_name"],
        "first_name": rec["first_name"],
        "middle_initial": None,
        "last_name": rec["last_name"],
        "name_suffix": None,
        "street_address": rec["street_address"],
        "city": rec["city"],
        "state": rec["state"],
        "zip_code": rec["zip_code"],
        "po_box": None,
        "attention_line": None,
        "frn": None,
        "applicant_type_code": rec["applicant_type_code"],
        "applicant_type": rec["applicant_type"],
        "radio_service_code": None,
        "radio_service": None,
        "grant_date": None,
        "expired_date": None,
        "convicted": None,
        "operator_class": op_class,
        "operator_class_desc": op_desc,
        "group_code": None,
        "region_code": None,
        "trustee_callsign": None,
        "trustee_indicator": None,
        "vanity_call_sign_change": None,
        "previous_callsign": None,
        "previous_operator_class": None,
        "trustee_name": rec["trustee_name"],
        "coordinates": None,
        "gridsquare": None,
        "geocode_match": None,
        "county": None,
        "dxcc_entity": None,
        "dxcc_id": None,
        "continent": None,
    }


def build_database(data_path, db_path):
    """Parse amateur_delim.txt into ca_amateur.sqlite; abort on any mismatch."""
    t0 = time.time()
    log(f"Building {os.path.basename(db_path)} from {os.path.basename(data_path)}")

    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    con.executescript(SCHEMA)

    ins = (f"INSERT INTO operators ({','.join(INSERT_COLS)}) "
           f"VALUES ({','.join('?' * len(INSERT_COLS))})")

    data_lines = 0        # non-header physical lines in the file
    batch, usi = [], 0
    with open(data_path, "r", encoding="utf-8", newline="") as fh:
        header = fh.readline()  # already validated; skip
        for line in fh:
            if not line.strip():
                continue      # tolerate a trailing blank line
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
    con.executescript(INDEXES)
    con.commit()

    # ---- verification (parallel to the FCC `counts` check) ----
    log("--- Build verification ---")
    ok = True
    n_ops = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    dup_cs = con.execute(
        "SELECT COUNT(*) FROM (SELECT callsign FROM operators "
        "GROUP BY callsign HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    n_club = con.execute(
        "SELECT COUNT(*) FROM operators WHERE applicant_type_code='B'"
    ).fetchone()[0]
    n_class = con.execute(
        "SELECT COUNT(*) FROM operators WHERE operator_class IS NOT NULL"
    ).fetchone()[0]
    status = "OK" if (n_ops == data_lines and dup_cs == 0) else "MISMATCH"
    if status != "OK":
        ok = False
    log(f"  data lines {data_lines:>8,}  rows stored {n_ops:>8,}  "
        f"duplicated callsigns {dup_cs}  {status}")
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


# --------------------------------------------------------------------------- #
# Phases 4-5 - geocode via the NRCan / geo.ca geolocation service, through a
# persistent content-addressed cache (mirrors the FCC pipeline's design).
# --------------------------------------------------------------------------- #

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
    """Accent-insensitive cache key for a town. The ISED source stores the same
    place several ways ('Trois-Rivières' / 'TROIS-RIVIERES' / ...); collapsing
    on the accent-stripped upper form lets every variant share one lookup."""
    return ("", strip_accents(norm(city)), norm(state))


def place_key(s):
    """Canonical form for comparing a place name from the ISED data against one
    from a geo.ca result title. Collapses everything the two sources disagree
    about: case, diacritics, punctuation, hyphen-vs-space, and the Saint/Sainte
    abbreviations ISED writes inconsistently. So 'MONTREAL-NORD' ==
    'Montréal-Nord', 'ST JOHNS' == "St. John's", 'STE-FOY' == 'Sainte-Foy'."""
    s = strip_accents(s).upper()
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"[.']", "", s)              # ST. JOHN'S -> ST JOHNS
    s = re.sub(r"[^A-Z0-9]+", " ", s)       # hyphens, en-dashes, slashes -> space
    s = re.sub(r"\bSAINTE\b", "STE", s)
    s = re.sub(r"\bSAINT\b", "ST", s)
    return re.sub(r"\s+", " ", s).strip()


def maidenhead(lat, lon, precision=6):
    """Maidenhead locator from decimal-degree lat/lon (default 6 chars)."""
    lon += 180.0
    lat += 90.0
    if not (0 <= lon < 360 and 0 <= lat < 180):
        return None
    loc = (chr(ord("A") + int(lon // 20)) + chr(ord("A") + int(lat // 10))
           + str(int((lon % 20) // 2)) + str(int((lat % 10) // 1)))
    if precision >= 6:
        loc += (chr(ord("a") + int((lon % 2) / (2 / 24)))
                + chr(ord("a") + int((lat % 1) / (1 / 24))))
    return loc[:precision]


_thread_local = threading.local()

# Set when the user presses Ctrl-C. Worker threads only ever run in the geocode
# phases; they watch this event so an interrupt stops them promptly instead of
# waiting out their retry backoff (the signal itself only reaches the main
# thread).
_INTERRUPTED = threading.Event()


def _session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "ca-callsign-geocoder/1.0"
        _thread_local.session = s
    return s


def geoloc(query, tries=4):
    """Query the geolocator. Return the candidate LIST on an authoritative
    HTTP-200 answer, or None when no answer was obtained.

    The distinction is the whole point (see NOTES.md 'transient vs no-match'):
    geo.ca has only two observable outcomes. A 200 always carries a JSON list of
    25 fuzzy candidates - even for nonsense - so a *list* is authoritative: if the
    caller's filters then reject all 25, that is a real no-match, safe to record.
    A 500 (its only error, ~half of requests, random and unrelated to the
    address) carries no information at all. We retry a few times with a SHORT
    backoff; if we still never saw a 200 list we return None, meaning 'never
    heard back' - a transient failure the caller must NOT persist as a miss, so
    it is retried next run rather than tombstoned for --miss-retry-days.

    None (transient) is thus distinct from a returned list the caller filters to
    nothing (real no-match). An empty/degraded 200 body is treated as no answer.
    """
    for attempt in range(tries):
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
        # Interruptible sleep: wakes immediately if Ctrl-C set the event.
        if _INTERRUPTED.wait(min(0.4 * (attempt + 1), 4.0)):
            return None
    return None


def _title_province(title):
    """Province name from a geo.ca result title: the last comma-segment, minus
    any trailing '(Kind)' parenthetical ('..., Nova Scotia (Town)' -> 'Nova
    Scotia')."""
    last = (title or "").rsplit(",", 1)[-1]
    return re.sub(r"\s*\(.*\)\s*$", "", last).strip()


def _title_kind(title):
    """Feature kind from a geo.ca result title: the trailing parenthetical
    ('Wesleyville, Bonavista North, Newfoundland and Labrador (Unincorporated
    area)' -> 'UNINCORPORATED AREA'). '' when the title carries none (streets
    and intersections do not)."""
    m = re.search(r"\(([^)]*)\)\s*\Z", title or "")
    return m.group(1).strip().upper() if m else ""


def _title_name(title):
    """Place name from a geo.ca result title: the first comma-segment. The
    middle segment may itself contain a ';' ('North Hatley; Memphrémagog'), but
    the name never does."""
    return (title or "").split(",")[0].strip()


def _coords(res):
    geom = res.get("geometry") or {}
    c = geom.get("coordinates")
    if c and len(c) == 2:
        lon, lat = c                          # service is longitude-first
        return float(lat), float(lon)
    return None


def street_variants(s):
    """Every plausible comparable form of a street name.

    A single canonical form cannot work: the same street arrives with a civic
    number on one side and without it on the other ('14088 66A Ave' queried,
    '66a Avenue' returned), and prairie-grid streets are themselves *named* by
    number ('4804 - 49 STREET'), so no fixed rule for how many leading numbers
    to drop is safe. Instead each reading is generated - dropping 0, 1 or 2
    leading numbers, with and without type words - and two names match when
    their variant sets intersect. Tokens are concatenated so 'FLAMING ROSE WAY'
    matches 'Flaming Roseway'.
    """
    s = strip_accents(s or "").upper()
    s = re.sub(r"[.'’`]", "", s)
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    toks = [t for t in s.split() if t]
    # ordinals -> bare number: 5TH -> 5, 62ND -> 62, 129E -> 129
    toks = [re.sub(r"^(\d+)(ST|ND|RD|TH|E|ER|RE)$", r"\1", t) for t in toks]
    toks = [STREET_ABBREV.get(t, t) for t in toks]
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
    # 'ST-IGNACE' vs 'Saint-Ignace': ST is ambiguous (Street / Saint), so emit
    # both readings rather than committing to one.
    saint = [re.sub(r"\A(SAINTE|STE)\Z", "STE", re.sub(r"\A(SAINT|ST)\Z", "ST", t))
             for t in toks]
    variants = set()
    for base in {tuple(toks), tuple(saint)}:
        for drop in (0, 1, 2):
            cur = list(base)
            for _ in range(drop):
                if cur and _NUMERIC_TOKEN.match(cur[0]):
                    cur.pop(0)
                else:
                    break
            for strip_types in (True, False):
                v = [t for t in cur if not (strip_types and (
                    t in STREET_TYPE_WORDS or t in STREET_DIRS))]
                while len(v) > 1 and _NUMERIC_TOKEN.match(v[-1]):
                    v.pop()
                v = [t[:-1] if len(t) > 4 and t.endswith("S") else t for t in v]
                key = "".join(v)
                if key:
                    variants.add(key)
    return variants


def pick_street(results, want_prov, want_street):
    """First Street/Address result in the wanted province whose STREET NAME
    matches the one queried -> (lat, lon, label).

    label: 'Street' for an exact civic match (INTERPOLATED_POSITION), else
    'Street_Approx' (street centroid). `want_prov` None (unknown province) skips
    the province check. Iterates candidates so a wrong top hit does not block a
    correct lower-ranked one.

    The name check matters for the same reason it does in pick_city: geo.ca
    answers an address it cannot resolve with an unrelated same-province street
    and labels it INTERPOLATED_POSITION, e.g. '383 Rue Hebert, Sherbrooke' ->
    'Rue Sherbrooke, Montreal'. Unlike the city picker this deliberately does
    NOT check the result's city, because geo.ca reports boroughs and amalgamated
    municipalities under the absorbing city (Scarborough -> City Of Toronto,
    Jonquiere -> Saguenay); a city check would reject thousands of correct
    matches. The residual that name-matching cannot catch is a right-street
    name in the wrong town - see NOTES.md.
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
            label = "Street" if res.get("qualifier") == "INTERPOLATED_POSITION" \
                else "Street_Approx"
            return (*latlon, label)
    return None


def pick_city(results, want_prov, want_city):
    """The Geoname that IS `want_city`, in `want_prov` -> (lat, lon,
    'City_Centroid').

    Unlike the street lookup, the province check alone is nowhere near enough
    here. geo.ca returns 25 fuzzy candidates for ANY query and has no "not
    found" signal, so a town it does not know matches whatever same-province
    noise ranks first — most often a lake (565 of 1,141 Geoname candidates in a
    120-query sample), sometimes the province itself. A candidate must therefore
    also:

      - NAME the town we asked for (compared through `place_key`), and
      - be a populated place, not a lake / river / province.

    Among same-named candidates a populated kind wins over a physical feature,
    so 'Meadow Lake (City)' beats 'Meadow Lake (Lake)'. Nothing qualifying ->
    None: an honest NULL beats a confidently wrong town hundreds of km away.
    """
    want_key = place_key(want_city)
    if not want_key:
        return None      # city is '-' / '.' / blank: nothing to validate against
    fallback = None
    for res in results or []:
        if not res.get("type", "").endswith("Geoname"):
            continue
        title = res.get("title", "")
        kind = _title_kind(title)
        if kind in REJECT_KINDS:
            continue
        if want_prov and _title_province(title).casefold() != want_prov.casefold():
            continue
        if place_key(_title_name(title)) != want_key:
            continue
        latlon = _coords(res)
        if not latlon:
            continue
        if kind in PLACE_KINDS:
            return (*latlon, "City_Centroid")
        if fallback is None:
            fallback = (*latlon, "City_Centroid")
    return fallback


# query_fn result meaning "geo.ca never answered (all attempts 500/timeout)".
# Distinct from a (lat, lon, label) hit and from None (a real no-match): a
# _TRANSIENT result is NOT written to the cache, so the address is retried on the
# next run instead of being tombstoned for --miss-retry-days.
_TRANSIENT = object()


def street_query(key, q):
    # key = (STREET, CITY, STATE) - see extract_distinct_streets()
    res = geoloc(q)
    if res is None:
        return _TRANSIENT
    return pick_street(res, PROVINCE_NAMES.get(key[2]), key[0])


def city_query(key, q):
    # key = ('', ACCENT-STRIPPED CITY, STATE) - see city_key()
    res = geoloc(q)
    if res is None:
        return _TRANSIENT
    return pick_city(res, PROVINCE_NAMES.get(key[2]), key[1])


def open_cache(cache_dir):
    """Open (creating if needed) the persistent content-addressed cache."""
    con = sqlite3.connect(os.path.join(cache_dir, CACHE_DB))
    con.execute(
        """
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
        )
        """
    )
    con.commit()
    return con


def load_cache(con, qkind):
    """{(street, city, state): (matched, lat, lon, quality, fetched_at)}."""
    out = {}
    for street, city, state, lat, lon, quality, matched, fetched_at in con.execute(
        "SELECT street, city, state, lat, lon, quality, matched, fetched_at "
        "FROM geocode_cache WHERE qkind=?", (qkind,)
    ):
        out[(street, city, state)] = (matched, lat, lon, quality, fetched_at)
    return out


def upsert_cache(con, qkind, rows):
    """rows: (street, city, state, lat, lon, quality, matched, fetched_at)."""
    con.executemany(
        """
        INSERT INTO geocode_cache
            (qkind, street, city, state, lat, lon, quality, matched, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(qkind, street, city, state) DO UPDATE SET
            lat=excluded.lat, lon=excluded.lon, quality=excluded.quality,
            matched=excluded.matched, fetched_at=excluded.fetched_at
        """,
        [(qkind, *r) for r in rows],
    )
    con.commit()


def _select_todo(cache, items, retry_before):
    """Split (key, query) items into (todo, hits, fresh_miss) using the cache."""
    todo, hits, fresh_miss = [], 0, 0
    for key, query in items:
        ent = cache.get(key)
        if ent is None:
            todo.append((key, query))
        elif ent[0]:
            hits += 1
        elif ent[4] < retry_before:
            todo.append((key, query))
        else:
            fresh_miss += 1
    return todo, hits, fresh_miss


def _run_pool(con, qkind, todo, query_fn, workers, now):
    """Look up each (key, query) in `todo` concurrently via `query_fn(key,
    query)`, committing to the cache in chunks so an interrupted run resumes.

    query_fn returns one of three things, and only the first two are cached:
      (lat, lon, label)  -> a hit               -> cached matched=1
      None               -> a real no-match     -> cached matched=0 (durable)
      _TRANSIENT         -> geo.ca never answered -> NOT cached, retried next run
    A transient failure is treated exactly like a miss FOR THIS RUN (the row
    falls through to the city/FSA/province fallbacks, since it is simply absent
    from the hit set), but leaving it out of the cache means the next run
    re-queries it instead of suppressing it for --miss-retry-days.

    Ctrl-C is handled promptly: pending (not-yet-started) lookups are cancelled,
    in-flight ones are told to stop (they abort their retry sleeps), progress so
    far is flushed, and the interrupt is re-raised. Because every cached lookup
    is committed in chunks, rerunning resumes where it left off.
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
    futs = {pool.submit(work, it): it for it in todo}
    try:
        for fut in as_completed(futs):
            try:
                key, hit = fut.result()
            except Exception:
                continue                    # worker crashed -> retry next run
            if key is None:
                continue                    # interrupted worker; nothing looked up
            if hit is _TRANSIENT:
                transient += 1              # no answer -> not cached, retried next run
                continue
            if hit:
                lat, lon, label = hit
                pending.append((*key, lat, lon, label, 1, now))
            else:
                pending.append((*key, None, None, None, 0, now))
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
    # A high transient rate means geo.ca was degraded/rate-limiting for this
    # run, not that these addresses are unresolvable - flag it so a bad run is
    # not mistaken for genuine coverage loss.
    if todo and transient / len(todo) > 0.5:
        log(f"  WARNING: {transient / len(todo):.0%} of {qkind} lookups never "
            f"got an answer - geo.ca looks degraded; rerun to fill them in.")


def extract_distinct_streets(db, limit=None):
    """[(key, query)] for distinct street addresses.

    key   = (STREET, CITY, STATE) upper-cased (cache/join key)
    query = "Street, City, State" in the source's original casing (geo.ca
            resolves natural-case addresses far more reliably than ALL-CAPS)

    Addresses with no civic street (PO box, rural route, general delivery) are
    skipped: there is nothing for a street geocoder to match, and geo.ca answers
    them with an arbitrary same-province street reported as a confident hit.
    They fall through to the Phase 5 town centroid, which is the best that can
    honestly be said about a PO box.
    """
    con = sqlite3.connect(db)
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

    key = accent-insensitive ('', CITY, STATE). Among the source's spellings of
    a town we query the one with the MOST accented characters, because geo.ca's
    place search needs the diacritics ('Trois-Rivières, QC' resolves; the
    accent-stripped 'TROIS-RIVIERES, QC' returns fuzzy garbage). This lets rows
    that were typed without accents piggyback on a correctly-accented variant.

    The best spelling is chosen from EVERY row of that town, not just the ones
    still needing a fallback. Those two sets differ: if all the rows spelled
    'Sept-Îles' were placed at street level, the only spellings left needing a
    town centroid are the unaccented 'SEPT-ILES' - and querying that returns
    nothing, so the accented sibling has to be found among rows that no longer
    need it. Which rows we QUERY FOR is still driven by what is unplaced.
    """
    con = sqlite3.connect(db)
    every = con.execute(
        "SELECT city, state FROM operators "
        "WHERE city IS NOT NULL AND TRIM(city) <> ''"
    ).fetchall()
    needed = con.execute(
        "SELECT DISTINCT city, state FROM operators "
        "WHERE coordinates IS NULL AND city IS NOT NULL AND TRIM(city) <> ''"
    ).fetchall()
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
    """UPDATE coordinates/gridsquare/geocode_match for rows whose (normalized)
    address is in `geo` ({key: (lat, lon, quality)}). Returns rows updated."""
    con = sqlite3.connect(db)
    updates = []
    for row in con.execute(
        f"SELECT unique_system_identifier, street_address, city, state "
        f"FROM operators WHERE {where}"
    ):
        pk = row[0]
        hit = geo.get(key_of(row))
        if hit:
            lat, lon, quality = hit
            updates.append((f"{lat:.6f},{lon:.6f}", maidenhead(lat, lon),
                            quality, pk))
    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?", updates,
    )
    con.commit()
    con.close()
    return len(updates)


def geocode_streets(db, cache_dir, workers, limit, miss_retry_days):
    """Phase 4: street-level geocode + write coordinates/gridsquare."""
    os.makedirs(cache_dir, exist_ok=True)
    con_cache = open_cache(cache_dir)
    cache = load_cache(con_cache, "street")
    now = time.time()
    retry_before = now - miss_retry_days * 86400.0

    items = extract_distinct_streets(db, limit)
    log(f"{len(items):,} distinct street addresses")
    todo, hits, fresh_miss = _select_todo(cache, items, retry_before)
    log(f"cache: {hits:,} matched reused, {fresh_miss:,} recent misses skipped, "
        f"{len(todo):,} to look up")

    if todo:
        _run_pool(con_cache, "street", todo, street_query, workers, now)
    cache = load_cache(con_cache, "street")
    con_cache.close()

    geo = {k: (v[1], v[2], v[3]) for k, v in cache.items() if v[0]}
    n = _write_coords(
        db, geo,
        "street_address IS NOT NULL AND TRIM(street_address) <> ''",
        lambda r: (norm(r[1]), norm(r[2]), norm(r[3])),
    )
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    con.close()
    log(f"{n:,}/{total:,} rows have street-level coordinates.")


def geocode_cities(db, cache_dir, workers, miss_retry_days):
    """Phase 5: town-centroid fallback for rows still without coordinates."""
    os.makedirs(cache_dir, exist_ok=True)
    con_cache = open_cache(cache_dir)
    cache = load_cache(con_cache, "city")
    now = time.time()
    retry_before = now - miss_retry_days * 86400.0

    items = extract_distinct_cities(db)
    log(f"{len(items):,} distinct city/province pairs need a fallback")
    todo, hits, fresh_miss = _select_todo(cache, items, retry_before)
    log(f"cache: {hits:,} matched reused, {fresh_miss:,} recent misses skipped, "
        f"{len(todo):,} to look up")

    if todo:
        _run_pool(con_cache, "city", todo, city_query, workers, now)
    cache = load_cache(con_cache, "city")
    con_cache.close()

    geo = {(k[1], k[2]): (v[1], v[2], v[3]) for k, v in cache.items() if v[0]}
    n = _write_coords(
        db, geo,
        "coordinates IS NULL AND city IS NOT NULL AND TRIM(city) <> ''",
        lambda r: city_key(r[2], r[3])[1:],   # (accent-stripped CITY, STATE)
    )
    con = sqlite3.connect(db)
    remaining = con.execute(
        "SELECT COUNT(*) FROM operators WHERE coordinates IS NULL"
    ).fetchone()[0]
    con.close()
    log(f"City fallback: {n:,} rows placed; "
        f"{remaining:,} rows remain without coordinates.")


# --------------------------------------------------------------------------- #
# Phase 8 - county (census division) assignment
# --------------------------------------------------------------------------- #

def assign_counties(db, cache_dir):
    """Fill `county` for every geocoded row via point-in-polygon against
    Canada's census divisions (the county-equivalent — counties, regional
    municipalities, districts, and numbered 'Division No. N' in the west).

    Stores the short CD name (`cd_short_name`), the direct analog of the FCC
    pipeline's short county name. Distinct coordinates are resolved once (many
    rows share a point); points inside no division (rounding / just offshore)
    snap to the nearest one.

    `Province_Centroid` rows (Phase 7) are excluded: their coordinate is a whole-
    province placeholder, so the census division it happens to fall in would be
    an arbitrary, wrong county. They keep `county` NULL - and therefore, for
    Ontario, a NULL section - rather than a fabricated one.
    """
    import shapely
    from shapely.strtree import STRtree
    import pyproj

    WHERE = ("coordinates IS NOT NULL AND county IS NULL "
             "AND geocode_match IS NOT 'Province_Centroid'")

    con = sqlite3.connect(db)
    if "county" not in [r[1] for r in con.execute("PRAGMA table_info(operators)")]:
        con.execute("ALTER TABLE operators ADD COLUMN county TEXT")

    coords = [r[0] for r in con.execute(
        f"SELECT DISTINCT coordinates FROM operators WHERE {WHERE}"
    )]
    if not coords:
        con.close()
        log("County: nothing to assign.")
        return
    log(f"Resolving county for {len(coords):,} distinct coordinate(s) ...")

    geoms, cduids, cdnames = load_cd_polygons(cache_dir)
    tree = STRtree(geoms)
    to3347 = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:3347", always_xy=True).transform

    lats, lons = [], []
    for c in coords:
        lat, lon = (float(x) for x in c.split(","))
        lats.append(lat); lons.append(lon)
    xs, ys = to3347(lons, lats)
    pts = shapely.points(xs, ys)

    county_of = [None] * len(pts)
    chunk = 50000
    for start in range(0, len(pts), chunk):
        pt_idx, poly_idx = tree.query(pts[start:start + chunk], predicate="within")
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
    rows = con.execute(
        f"SELECT unique_system_identifier, coordinates FROM operators WHERE {WHERE}"
    ).fetchall()
    con.executemany(
        "UPDATE operators SET county=? WHERE unique_system_identifier=?",
        [(coord_county[c], pk) for pk, c in rows if c in coord_county],
    )
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM operators WHERE county IS NOT NULL").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    con.close()
    log(f"County: {n:,}/{total:,} rows assigned.")


# --------------------------------------------------------------------------- #
# Phase 6 - postal-code (FSA) cross-check
# --------------------------------------------------------------------------- #

def fetch_boundary_file(url, zpath, label):
    """Download a StatCan boundary zip to `zpath` once, and reuse it thereafter.

    Verified before it is kept, because StatCan serves a missing file as an
    HTML error page under HTTP **200** (`/census-recensement/srvmsg/
    srvmsg404.html`), which raise_for_status() cannot see. Without the check a
    retired URL would leave a 4 KB HTML file named .zip in the cache, and every
    later run would 'reuse' it and fail somewhere far less obvious. The 2021
    files are current - there is no 2026 Census release yet (its data products
    run to Fall 2028) - but these URLs will eventually move.
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
            head = ""
            try:
                with open(tmp, "rb") as f:
                    head = f.read(200).decode("utf-8", "replace")
            except Exception:
                pass
            sys.exit(f"ERROR: {url} did not return a zip file "
                     f"({os.path.getsize(tmp):,} bytes). StatCan serves missing "
                     f"files as HTTP 200 HTML, so this usually means the URL has "
                     f"moved - check the boundary-file page for the current "
                     f"release.\n  starts with: {head[:120]!r}")
        os.replace(tmp, zpath)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)          # never leave a partial/bogus file behind
    log(f"  {os.path.getsize(zpath) / 1e6:,.1f} MB -> {zpath}")
    return zpath


def load_fsa_polygons(cache_dir):
    """{FSA: shapely geometry} in the StatCan file's native CRS (EPSG:3347).
    Downloads the boundary file once into cache_dir and reuses it thereafter."""
    import shapefile as pyshp
    from shapely.geometry import shape as shapely_shape

    zpath = fetch_boundary_file(STATCAN_FSA_URL,
                                os.path.join(cache_dir, STATCAN_FSA_ZIP),
                                "FSA boundary file")
    with zipfile.ZipFile(zpath) as zf:
        stem = next(n for n in zf.namelist() if n.endswith(".shp"))[:-4]
        rdr = pyshp.Reader(
            shp=io.BytesIO(zf.read(stem + ".shp")),
            shx=io.BytesIO(zf.read(stem + ".shx")),
            dbf=io.BytesIO(zf.read(stem + ".dbf")),
            encoding="latin-1",
        )
        flds = [f[0] for f in rdr.fields[1:]]
        out = {}
        for sr in rdr.iterShapeRecords():
            d = dict(zip(flds, sr.record))
            fsa = str(d["CFSAUID"]).strip().upper()
            g = shapely_shape(sr.shape.__geo_interface__)
            out[fsa] = g if fsa not in out else out[fsa].union(g)
    log(f"{len(out):,} FSA polygons loaded")
    return out


def postal_check(db, cache_dir):
    """Cross-check every coordinate against the row's own postal code, and place
    rows that have a postal code but no coordinate.

    This is the only geographic evidence in the pipeline that does NOT come from
    geo.ca, which is what makes it worth the download: the street and city
    pickers can only ask whether a returned name looks right, so neither can
    catch a correct street name in the wrong town. The postal code can.

    A coordinate is replaced by its FSA's interior point when it lies further
    outside the FSA than the FSA's own radius - the point at which the FSA
    estimate is provably the closer of the two. That also captures the honest
    case where a long street's centroid is simply not near where the licensee
    lives. Urban FSAs have a median radius of 3.0 km, so 'FSA_Centroid' is
    typically finer-grained than 'City_Centroid'; rural ones (median 43.4 km)
    are coarse, and the radius-based rule stops them overriding anything better.
    """
    import shapely
    from shapely.strtree import STRtree
    import pyproj

    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT unique_system_identifier, substr(upper(zip_code),1,3), "
        "       coordinates, state, geocode_match FROM operators "
        "WHERE zip_code GLOB '[A-Za-z][0-9][A-Za-z]*'"
    ).fetchall()
    if not rows:
        con.close()
        log("Postal check: no usable postal codes.")
        return

    fsa_geom = load_fsa_polygons(cache_dir)
    to3347 = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:3347", always_xy=True).transform
    to4326 = pyproj.Transformer.from_crs(
        "EPSG:3347", "EPSG:4326", always_xy=True).transform

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
            continue
        if state and state not in POSTAL_PROVINCE.get(fsa[0], set()):
            skipped_prov += 1          # postal code and province disagree
            continue
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
        idx_of = {f: i for i, f in enumerate(keys)}
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

    filled = 0
    for pk, fsa, coords, gm in missing:
        lat, lon = interior[fsa]
        updates.append((f"{lat:.6f},{lon:.6f}", maidenhead(lat, lon),
                        "FSA_Centroid", pk))
        filled += 1

    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?", updates)
    con.commit()

    corrected = sum(corrected_by_kind.values())
    log(f"  corrected {corrected:,} coordinate(s) that contradicted their "
        f"postal code:")
    for kind, n in sorted(corrected_by_kind.items(), key=lambda kv: -kv[1]):
        log(f"      was {str(kind):>14}: {n:>7,}")
    log(f"  placed {filled:,} previously-unplaced row(s) from their postal code")
    remaining = con.execute(
        "SELECT COUNT(*) FROM operators WHERE coordinates IS NULL").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    con.close()
    log(f"  {total - remaining:,}/{total:,} rows now have coordinates "
        f"({remaining:,} still without)")


# --------------------------------------------------------------------------- #
# Phase 7 - province-centroid last resort
# --------------------------------------------------------------------------- #

def load_province_points(cache_dir):
    """{province code: (lat, lon)} - an interior point of each province, unioned
    from the census-division polygons. Reuses the census-division file the
    county phase already needs, so it adds no download."""
    import shapely  # noqa: F401  (imported so pyproj/shapely are proven present)
    from shapely.geometry import shape as shapely_shape
    from shapely.ops import unary_union
    import shapefile as pyshp
    import pyproj

    zpath = fetch_boundary_file(STATCAN_CD_URL,
                                os.path.join(cache_dir, STATCAN_CD_ZIP),
                                "census-division boundary file")
    by_prov = {}
    with zipfile.ZipFile(zpath) as zf:
        base = STATCAN_CD_ZIP[:-4]
        rdr = pyshp.Reader(
            shp=io.BytesIO(zf.read(base + ".shp")),
            shx=io.BytesIO(zf.read(base + ".shx")),
            dbf=io.BytesIO(zf.read(base + ".dbf")),
            encoding="latin-1",
        )
        flds = [f[0] for f in rdr.fields[1:]]
        for sr in rdr.iterShapeRecords():
            d = dict(zip(flds, sr.record))
            prov = PRUID_TO_PROV.get(str(d["PRUID"]))
            if prov:
                by_prov.setdefault(prov, []).append(
                    shapely_shape(sr.shape.__geo_interface__))
    to4326 = pyproj.Transformer.from_crs(
        "EPSG:3347", "EPSG:4326", always_xy=True).transform
    points = {}
    for prov, geoms in by_prov.items():
        p = unary_union(geoms).representative_point()   # guaranteed inside
        lon, lat = to4326(p.x, p.y)
        points[prov] = (lat, lon)
    log(f"{len(points)} province interior point(s) derived")
    return points


def assign_province_fallback(db, cache_dir):
    """Phase 7: the last-resort placement. Any row still without coordinates but
    carrying a province gets that province's interior point, labelled
    'Province_Centroid'.

    This is deliberately coarse - it says no more than the `state` column
    already does - so these rows are EXCLUDED from the county assignment
    (Phase 8), and therefore from the Ontario section split, rather than being
    handed a made-up census division. Non-Ontario rows still receive their RAC
    section, which is a whole-province value regardless of where in the province
    the point sits. Rows with no province at all cannot be placed and stay NULL.
    """
    con = sqlite3.connect(db)
    todo = con.execute(
        "SELECT unique_system_identifier, TRIM(UPPER(state)) FROM operators "
        "WHERE coordinates IS NULL AND state IS NOT NULL AND TRIM(state) <> ''"
    ).fetchall()
    if not todo:
        con.close()
        log("Province fallback: nothing to place.")
        return

    points = load_province_points(cache_dir)
    updates, unknown = [], 0
    for pk, st in todo:
        pt = points.get(st)
        if not pt:
            unknown += 1
            continue
        lat, lon = pt
        updates.append((f"{lat:.6f},{lon:.6f}", maidenhead(lat, lon),
                        "Province_Centroid", pk))
    con.executemany(
        "UPDATE operators SET coordinates=?, gridsquare=?, geocode_match=? "
        "WHERE unique_system_identifier=?", updates)
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM operators").fetchone()[0]
    remaining = con.execute(
        "SELECT COUNT(*) FROM operators WHERE coordinates IS NULL").fetchone()[0]
    con.close()
    log(f"Province fallback: {len(updates):,} row(s) placed at their province's "
        f"interior point (Province_Centroid)"
        + (f"; {unknown:,} had an unrecognised province code" if unknown else ""))
    log(f"  {total - remaining:,}/{total:,} rows now have coordinates "
        f"({remaining:,} still without - no province at all)")


# --------------------------------------------------------------------------- #
# Phase 9 - DXCC entity (all Canada)
# --------------------------------------------------------------------------- #

def assign_dxcc(db):
    """Every Canadian amateur license is DXCC entity 'Canada' (1)."""
    con = sqlite3.connect(db)
    name, num = DXCC_CANADA
    con.execute("UPDATE operators SET dxcc_entity=?, dxcc_id=?", (name, num))
    con.commit()
    log("DXCC entity breakdown:")
    for ent, n in con.execute(
        "SELECT COALESCE(dxcc_entity, '(undetermined)'), COUNT(*) "
        "FROM operators GROUP BY dxcc_entity ORDER BY COUNT(*) DESC"
    ):
        log(f"  {ent:>26}: {n:>9,}")
    con.close()


# --------------------------------------------------------------------------- #
# Phase 10 - continent
# --------------------------------------------------------------------------- #

def assign_continent(db):
    """Canada is entirely North America; continent = 'NA' where dxcc_id is set."""
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE operators SET continent=? WHERE dxcc_id IS NOT NULL",
        (CONTINENT_CANADA,),
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
# Census-division boundary helpers (used by Phase 8 county assignment)
# --------------------------------------------------------------------------- #

def cd_short_name(cdname):
    """Short census-division name: drop StatCan's bilingual 'English / French'
    duplication ('Greater Sudbury / Grand Sudbury' -> 'Greater Sudbury') and
    collapse its padding whitespace ('Division No.  6' -> 'Division No. 6')."""
    name = (cdname or "").split(" / ")[0]
    return re.sub(r"\s+", " ", name).strip() or None


def load_cd_polygons(cache_dir, only_pruid=None):
    """(geoms, cduids, cdnames) for census divisions, in the StatCan file's
    native CRS (EPSG:3347). Downloads the boundary file once into cache_dir, and
    reuses it on later runs. `only_pruid` (e.g. '35' for Ontario) restricts to
    one province."""
    import shapefile as pyshp
    from shapely.geometry import shape as shapely_shape

    zpath = fetch_boundary_file(STATCAN_CD_URL,
                                os.path.join(cache_dir, STATCAN_CD_ZIP),
                                "census-division boundary file")
    with zipfile.ZipFile(zpath) as zf:
        base = STATCAN_CD_ZIP[:-4]
        rdr = pyshp.Reader(
            shp=io.BytesIO(zf.read(base + ".shp")),
            shx=io.BytesIO(zf.read(base + ".shx")),
            dbf=io.BytesIO(zf.read(base + ".dbf")),
            encoding="latin-1",   # StatCan dbf holds latin-1 French names
        )
        flds = [f[0] for f in rdr.fields[1:]]
        geoms, cduids, cdnames = [], [], []
        for sr in rdr.iterShapeRecords():
            d = dict(zip(flds, sr.record))
            if only_pruid and str(d["PRUID"]) != only_pruid:
                continue
            geoms.append(shapely_shape(sr.shape.__geo_interface__))
            cduids.append(str(d["CDUID"]))
            cdnames.append(cd_short_name(d["CDNAME"]))
    scope = f"province {only_pruid}" if only_pruid else "Canada"
    log(f"{len(geoms)} census-division polygons loaded ({scope})")
    return geoms, cduids, cdnames


# --------------------------------------------------------------------------- #
# Phase 11 - ARRL/RAC section (pure lookup off province + county)
# --------------------------------------------------------------------------- #

def assign_arrl_section(db):
    """Fill `arrl_section` (RAC section used in ARRL contests) - a pure lookup,
    no geometry.

    Every province maps 1:1 by `state` code except the three territories (shared
    'TER') and Ontario, whose four sections (GH/ONE/ONN/ONS) are looked up from
    the `county` (census division) that Phase 8 already resolved. So Ontario
    sections depend on Phase 8 having run; Ontario rows with a NULL county
    (no coordinates, --no-county, or a Province_Centroid placement) stay NULL.
    """
    con = sqlite3.connect(db)
    if "arrl_section" not in [r[1] for r in con.execute("PRAGMA table_info(operators)")]:
        con.execute("ALTER TABLE operators ADD COLUMN arrl_section TEXT")
    con.execute("UPDATE operators SET arrl_section = NULL")

    # non-Ontario: direct province/territory -> section
    con.executemany(
        "UPDATE operators SET arrl_section=? WHERE state=?",
        [(sec, pr) for pr, sec in SECTION_BY_PROVINCE.items()],
    )
    # Ontario: look the section up from the census division in `county`
    con.executemany(
        "UPDATE operators SET arrl_section=? WHERE state='ON' AND county=?",
        [(sec, name) for name, sec in ON_COUNTY_SECTION.items()],
    )
    con.commit()

    on_missing = con.execute(
        "SELECT COUNT(*) FROM operators "
        "WHERE state='ON' AND county IS NOT NULL AND arrl_section IS NULL"
    ).fetchone()[0]
    if on_missing:
        log(f"  WARNING: {on_missing} Ontario row(s) have a county not in "
            f"ON_COUNTY_SECTION (unmapped) - left NULL")

    log("ARRL/RAC section breakdown:")
    for sec, n in con.execute(
        "SELECT COALESCE(arrl_section, '(none)'), COUNT(*) FROM operators "
        "GROUP BY arrl_section ORDER BY COUNT(*) DESC"
    ):
        log(f"  {sec:>8}: {n:>9,}")
    con.close()


# --------------------------------------------------------------------------- #
# Phase 12 - finalize
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
        log("")
        log("-" * 70)
        log(" NOTE: the finished database could not replace the previous one")
        log("")
        log(f"   {type(e).__name__}: {e}")
        log("")
        log("   This is normally another process holding the old file open")
        log("   (a SQLite browser, an editor, a backup agent).")
        log("")
        log("   The new database is COMPLETE and sits at:")
        log(f"     {db}")
        log("")
        log("   Close whatever holds the old file and rename it by hand, or")
        log("   just rerun once nothing else has it open.")
        log("-" * 70)
        log("")
        return
    log(f"{'Replaced' if replacing else 'Created'} {final_db}"
        f"{' (previous version discarded)' if replacing else ''}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

# Phases 6, 7 and 8 import shapely/pyshp/pyproj lazily, at the point they need
# them - which is hours into a cold run, after the download, the build, and the
# whole geo.ca street + city geocode. Discovering a missing package there costs
# all of that work, so check up front instead: the module is only imported to
# prove it is installed, and nothing here does any work.
#
# Only the phases actually enabled are checked, so `--no-postal --no-province
# --no-county` still runs with nothing but `requests` installed.
def preflight(args):
    """Abort before Phase 1 if an enabled phase's packages are missing."""
    geo_phases = []
    if not args.no_postal:
        geo_phases.append("6 (postal/FSA)")
    if not args.no_province:
        geo_phases.append("7 (province)")
    if not args.no_county:
        geo_phases.append("8 (county)")
    if not geo_phases:
        return

    # pip name != import name for pyshp, and the message has to name what you
    # would actually type.
    pip_names = {"shapely": "shapely", "shapefile": "pyshp", "pyproj": "pyproj"}
    missing = []
    for mod in ("shapely", "shapefile", "pyproj"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_names[mod])
    if not missing:
        return

    sys.exit(
        "ERROR: missing required package(s) for the phases you asked for:\n"
        f"  {', '.join(missing)}\n"
        f"  needed by Phase {'; Phase '.join(geo_phases)}\n"
        "\nInstall them:\n"
        f"  python -m pip install {' '.join(missing)}\n"
        "  (or: python -m pip install -r ../requirements.txt)\n"
        "\nOr skip the phases that need them:\n"
        "  python update_ca_db.py --no-postal --no-province --no-county\n"
        "  - coordinates still come from Phases 4-5; county, the Ontario\n"
        "    sections, and the FSA cross-check stay NULL."
    )


def main():
    global _log_fh

    ap = argparse.ArgumentParser(
        description="Canadian amateur callsign database refresh: cleanup, "
                    "download, build (FCC-identical schema), DXCC, continent.")
    ap.add_argument("--cache-dir", default=os.path.join(HERE, "geocode_cache"))
    ap.add_argument("--workers", type=int, default=5,
                    help="concurrent geocode requests (default 5; geo.ca "
                         "throttles per-IP so more workers rarely help)")
    ap.add_argument("--limit", type=int, default=None,
                    help="geocode only the first N distinct addresses (testing)")
    ap.add_argument("--miss-retry-days", type=float, default=30.0,
                    help="re-query a cached miss older than this many days")
    ap.add_argument("--no-geocode", action="store_true",
                    help="skip Phases 4-5 (leave coordinates NULL)")
    ap.add_argument("--no-postal", action="store_true",
                    help="skip Phase 6 (postal-code / FSA cross-check + fill)")
    ap.add_argument("--no-province", action="store_true",
                    help="skip Phase 7 (province-centroid last resort)")
    ap.add_argument("--no-county", action="store_true",
                    help="skip Phase 8 (county / census-division assignment)")
    ap.add_argument("--no-dxcc", action="store_true", help="skip Phase 9")
    ap.add_argument("--no-continent", action="store_true", help="skip Phase 10")
    ap.add_argument("--no-section", action="store_true",
                    help="skip Phase 11 (ARRL/RAC section)")
    args = ap.parse_args()

    # Before anything is downloaded, built, or geocoded.
    preflight(args)

    # Paths are fixed (DB_PATH / WORK_DB / ZIP_PATH, all beside the script).
    work_db, db = WORK_DB, DB_PATH

    _log_fh = open(os.path.join(HERE, RUN_LOG), "w", encoding="utf-8")
    t0 = time.time()
    log(f"=== Canadian amateur database refresh started ({time.strftime('%Y-%m-%d')}) ===")
    log(f"Building into {work_db}")
    log(f"  -> becomes {db} on success"
        f"{'' if os.path.exists(db) else ' (no previous database present)'}")

    log("--- Phase 1: cleanup ---")
    cleanup_old_data()

    log("--- Phase 2: download + extract + validate ---")
    download_ca_zip(ZIP_PATH)
    data_path = extract_and_validate(ZIP_PATH)

    log("--- Phase 3: build database ---")
    build_database(data_path, work_db)

    log("--- Phase 4: geocode street addresses (geo.ca) ---")
    if args.no_geocode:
        log("skipped (--no-geocode)")
    else:
        geocode_streets(work_db, args.cache_dir, args.workers, args.limit,
                        args.miss_retry_days)

    log("--- Phase 5: city-centroid fallback ---")
    if args.no_geocode:
        log("skipped (--no-geocode)")
    else:
        geocode_cities(work_db, args.cache_dir, args.workers,
                       args.miss_retry_days)

    log("--- Phase 6: postal-code (FSA) cross-check ---")
    if args.no_postal:
        log("skipped (--no-postal)")
    else:
        postal_check(work_db, args.cache_dir)

    log("--- Phase 7: province-centroid last resort ---")
    if args.no_province:
        log("skipped (--no-province)")
    else:
        assign_province_fallback(work_db, args.cache_dir)

    log("--- Phase 8: county (census division) assignment ---")
    if args.no_county:
        log("skipped (--no-county)")
    else:
        assign_counties(work_db, args.cache_dir)

    log("--- Phase 9: DXCC entity ---")
    if args.no_dxcc:
        log("skipped (--no-dxcc)")
    else:
        assign_dxcc(work_db)

    log("--- Phase 10: continent ---")
    if args.no_continent:
        log("skipped (--no-continent)")
    else:
        assign_continent(work_db)

    log("--- Phase 11: ARRL/RAC section ---")
    if args.no_section:
        log("skipped (--no-section)")
    else:
        if args.no_county:
            log("note: Ontario sections need Phase 8 (county); ON stays NULL")
        assign_arrl_section(work_db)

    log("--- Phase 12: finalize ---")
    finalize(work_db, db)

    log(f"=== SUCCESS: {db} in {(time.time() - t0) / 60:,.1f} minutes ===")
    _log_fh.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _INTERRUPTED.set()
        log("Interrupted by user (Ctrl-C). Any completed geocode lookups are "
            "cached; rerun the same command to resume.")
        try:
            if _log_fh:
                _log_fh.flush()
                _log_fh.close()
        except Exception:
            pass
        # A plain sys.exit() would still hang: at interpreter shutdown the
        # ThreadPoolExecutor's atexit hook JOINS every worker thread, and a
        # worker blocked in a slow geo.ca network read won't return until its
        # ~60s socket timeout. Progress is already committed to the cache, so
        # terminate immediately instead of waiting on those threads.
        os._exit(130)
