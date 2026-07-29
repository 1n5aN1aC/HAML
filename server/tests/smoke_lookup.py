"""End-to-end smoke test for the callsign-lookup feature.

The lookup chain (`lookup.SOURCES`) is offline today: the FCC ULS sqlite,
the ISED (Canadian) sqlite, and the CallParser prefix DB. No shipped source
is cacheable, so no lookup writes a cache row.

Stdlib-only. Spawns the real server on a scratch port with a scratch data
dir + a scratch FCC ULS fixture sqlite + a scratch ultracheck fixture sqlite +
the repo's committed Prefix.lst, then walks POST /api/lookup:

  - cold Individual: 200 with composed name "FIRST M LAST", license_type
    "person", address_line2 matching the client's state regex, derived
    zones, ISO dates, source "fcc"
  - warm re-hit: 200, same record
  - suffix normalization (W1AW/P): 200, same record
  - cold Amateur Club: 200, license_type "club", entity_name
  - PO-box-only licensee: address_line1 == "PO BOX 123"
  - NULL coordinates: 200, latitude/longitude None, zones None
  - cold unknown call (no prefix match either): 200 with found:false and an
    otherwise-null record — a miss is an answer, not an HTTP error
  - ultracheck on every response: exact-match-first ahead of each source's own
    ordering, per-source limits and the `truncated` flag, a partial term with no
    exact match, and the degrade path when the dataset is absent. Runs against a
    scratch ultracheck fixture — never the real 91 MB build
  - DX call (G4ABC, not in the FCC fixture): 200 via CallParser —
    source "callparser", DXCC-level fields only, US-only fields null,
    distance from entity-center coords
  - FCC still wins on its own fixture rows (source stays "fcc")
  - portable prefix (EA8/W1AW): 200 via CallParser, Canary Is.
  - bad input (empty): 400
  - bad input (non-JSON): 400
  - missing-DB config: prefix-resolvable calls (incl. US) now 200 via
    CallParser; a call neither hop resolves keeps the 502 visible
  - missing-DB + missing-Prefix.lst config: 502 (today's behavior exactly)
  - coalescing: two concurrent POSTs for the same cold callsign only
    resolve once
  - unit checks: TTL policy, coerce() contract (incl. ISO date acceptance),
    post-processing (zone derivation + distance stamping), source-chain
    shape + CACHED flags, chain fall-through rules against stub sources,
    fcc adapter row -> canonical mapping, callparser adapter hit ->
    canonical mapping (incl. not-ready setup semantics)
  - unit checks over the shipped dataset: the location derivations
    (recalculate() registry, gridsquare both ways, POTA park, section and
    state anchors, section-from-state, blank()) and the operator-override
    chain in lookup_postprocess.apply() (precedence, what each override
    blanks, and the atomicity that lets an unresolvable one fall through)

No internet access required. The end-to-end fixture sqlite is built in
scratch, not the real dataset; the prefix DB is the small committed
server/datasets/Prefix.lst.

The two location unit blocks are the exception: `lookup_location_calc`
opens the configured dataset itself, so they read the real
`datasets/lookup_data.sqlite` and need it present. Polygon answers are
asserted by value, anchors only by property — a section anchor moves with
every ULS dump, so what is checked is which section it lands in.

Run: python server/tests/smoke_lookup.py
"""
import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

import aiohttp

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
import lookup_cache        # noqa: E402
import lookup_record       # noqa: E402
import lookup_ultracheck   # noqa: E402

PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"

# --- client state-parse contract ------------------------------------------
# Mirrors client/src/lookup-fill.js so the test asserts the same regex the
# client uses to fill the state field.
STATE_IN_ADDRESS_RE = re.compile(r"\b([A-Z]{2})\s+\d{5}\b")
VALID_STATES = {
    'AB','AK','AL','AR','AZ','BC','CA','CO','CT','DC','DE','DX','FL','GA',
    'HI','IA','ID','IL','IN','KS','KY','LA','MA','MB','MD','ME','MI','MN',
    'MO','MS','MT','NB','NC','ND','NE','NH','NJ','NL','NM','NS','NT','NU',
    'NV','NY','OH','OK','ON','OR','PA','PE','QC','RI','SC','SD','SK','TN',
    'TX','UT','VA','VT','WA','WI','WV','WY','YT',
}

# The source keys every ultracheck object must carry. Spelled out rather than
# read off lookup_ultracheck._SOURCES so a rename or a dropped source fails
# here instead of quietly agreeing with itself — these names are wire contract.
_UC_SOURCES = ("fd", "wfd", "pota_hunter", "pota_activator",
               "lotw", "clublog", "scp")

checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ok: {label}")


# A miss body must be the SAME wire shape as a hit — that is the whole reason
# it can be a 200 — so it is asserted structurally rather than field by field:
# every canonical field present, every one null but `callsign`, and the
# request-time extras there too. A client merging this record must find nothing
# in it to fill, so a single stray non-null value is a real failure.
def _check_miss_shape(body, callsign):
    missing = [f for f in lookup_record.FIELDS if f not in body]
    check(not missing,
          f"miss body has every canonical field (missing {missing})")
    non_null = {f: body[f] for f in lookup_record.FIELDS
                if f != "callsign" and body.get(f) is not None}
    check(not non_null,
          f"miss body is null but for the callsign (got {non_null})")
    for extra in ("found", "distance", "pota_park", "ultracheck"):
        check(extra in body, f"miss body has the {extra} extra")


# Assert one ultracheck source's list against the FULL expected fixture order,
# clipped to that source's configured limit — and that `truncated` agrees with
# whether the clip actually dropped anything. SOURCE_LIMITS is tuning, not
# contract, so reading it here is what keeps these checks about ORDERING
# instead of quietly re-asserting today's limit values.
def _uc_expect(sources, name, full_order, why):
    limit = lookup_ultracheck.SOURCE_LIMITS[name]
    got = [m["callsign"] for m in sources[name]["matches"]]
    check(got == full_order[:limit],
          f"ultracheck {name}: {why} (got {got}, "
          f"expected {full_order[:limit]} at limit {limit})")
    check(sources[name]["truncated"] is (len(full_order) > limit),
          f"ultracheck {name}: truncated is "
          f"{len(full_order) > limit} at limit {limit} with "
          f"{len(full_order)} matches (got {sources[name]['truncated']!r})")


# --- fixture ---------------------------------------------------------------
# Mirror of the production operator-table schema (see server/datasets/README.md).
# Both `fcc_operators` and `ca_operators` share this layout, so the template
# is filled in per table. Real DB has 826k + 92k rows; the test only needs a
# handful to exercise the adapters' mapping + zones + status paths.
OPERATORS_SCHEMA = """
CREATE TABLE {table} (
  callsign              TEXT PRIMARY KEY,
  applicant_type        TEXT,
  first_name            TEXT,
  middle_initial        TEXT,
  last_name             TEXT,
  name_suffix           TEXT,
  entity_name           TEXT,
  operator_class        TEXT,
  previous_operator_class TEXT,
  previous_callsign     TEXT,
  trustee_callsign      TEXT,
  trustee_name          TEXT,
  street_address        TEXT,
  po_box                TEXT,
  city                  TEXT,
  state                 TEXT,
  zip_code              TEXT,
  attention_line        TEXT,
  frn                   TEXT,
  grant_date            TEXT,
  expired_date          TEXT,
  gridsquare            TEXT,
  coordinates           TEXT,
  county                TEXT,
  arrl_section          TEXT,
  dxcc_entity           TEXT,
  continent             TEXT,
  dxcc_id               INTEGER
);
"""

# Fixture rows. Coordinates pick known locations so the expected zones are
# stable across the polygon files we vendor:
#   W1AW:  Dallas, OR     (44.98, -123.34) — CQ 3, ITU 6
#   K1MI:  Portland, OR   (45.52, -122.68) — CQ 3, ITU 6
#   W7CLB: Portland, OR   (45.52, -122.68) — CQ 3, ITU 6
#   N0BOX: Eugene, OR     (44.05, -123.09) — CQ 3, ITU 6
#   N0GEO: no coordinates
FCC_FIXTURE = [
    # W1AW: Individual, has coords + previous_callsign (KG7WKU is NOT a row
    # in the table — proves a miss for a "previous" value). entity_name is
    # "MONKS, WILLIAM S" — proves the adapter builds the name from the
    # component fields, not the entity column (which would feed the client
    # the wrong first token).
    {
        "callsign": "W1AW",
        "applicant_type": "Individual",
        "first_name": "JOSHUA", "middle_initial": "D", "last_name": "VILLWOCK",
        "name_suffix": "",
        "entity_name": "MONKS, WILLIAM S",
        "operator_class": "E", "previous_operator_class": "G",
        "previous_callsign": "KG7WKU",
        "trustee_callsign": "", "trustee_name": "",
        "street_address": "14970 SALT CREEK RD", "po_box": "",
        "city": "DALLAS", "state": "OR", "zip_code": "97338",
        "attention_line": "",
        "frn": "0024933376",
        "grant_date": "2024-03-19", "expired_date": "2034-03-19",
        "gridsquare": "CN84hx",
        "coordinates": "44.979441,-123.337862",
        "county": "Polk",
        "arrl_section": "OR",
        "dxcc_entity": "United States",
        "continent": "NA",
        "dxcc_id": 291,
    },
    # K1MI: Individual, has coords, no previous call. Used to prove the
    # previous_callsign field surfaces when set, and absent otherwise.
    {
        "callsign": "K1MI",
        "applicant_type": "Individual",
        "first_name": "TEST", "middle_initial": "", "last_name": "USER",
        "name_suffix": "",
        "entity_name": "",
        "operator_class": "G", "previous_operator_class": "",
        "previous_callsign": "",
        "trustee_callsign": "", "trustee_name": "",
        "street_address": "1 TEST ST", "po_box": "",
        "city": "PORTLAND", "state": "OR", "zip_code": "97201",
        "attention_line": "",
        "frn": "0024933376",
        "grant_date": "2020-01-01", "expired_date": "2030-01-01",
        "gridsquare": "CN85",
        "coordinates": "45.5152,-122.6784",
        "county": "Multnomah",
        "arrl_section": "OR",
        "dxcc_entity": "United States",
        "continent": "NA",
        "dxcc_id": 291,
    },
    # W7CLB: Amateur Club with trustee. License_class is empty for clubs;
    # trustee_callsign populates the trustee fields the client displays.
    {
        "callsign": "W7CLB",
        "applicant_type": "Amateur Club",
        "first_name": "", "middle_initial": "", "last_name": "",
        "name_suffix": "",
        "entity_name": "TEST RADIO CLUB",
        "operator_class": "", "previous_operator_class": "",
        "previous_callsign": "",
        "trustee_callsign": "W7TRU", "trustee_name": "TEST TRUSTEE",
        "street_address": "100 CLUB LN", "po_box": "",
        "city": "PORTLAND", "state": "OR", "zip_code": "97201",
        "attention_line": "",
        "frn": "",
        "grant_date": "2000-01-01", "expired_date": "2030-01-01",
        "gridsquare": "CN85",
        "coordinates": "45.5152,-122.6784",
        "county": "Multnomah",
        "arrl_section": "OR",
        "dxcc_entity": "United States",
        "continent": "NA",
        "dxcc_id": 291,
    },
    # N0BOX: PO-box-only licensee (no street_address). The adapter must
    # synthesize "PO BOX {po_box}" so the entry form has something usable.
    {
        "callsign": "N0BOX",
        "applicant_type": "Individual",
        "first_name": "BOX", "middle_initial": "", "last_name": "PERSON",
        "name_suffix": "",
        "entity_name": "",
        "operator_class": "T", "previous_operator_class": "",
        "previous_callsign": "",
        "trustee_callsign": "", "trustee_name": "",
        "street_address": "", "po_box": "123",
        "city": "EUGENE", "state": "OR", "zip_code": "97401",
        "attention_line": "",
        "frn": "",
        "grant_date": "2010-01-01", "expired_date": "2030-01-01",
        "gridsquare": "CN84",
        "coordinates": "44.0521,-123.0868",
        "county": "Lane",
        "arrl_section": "or",
        "dxcc_entity": "United States",
        "continent": "NA",
        "dxcc_id": 291,
    },
    # N0GEO: NULL coordinates. latitude/longitude/zones must all be None.
    {
        "callsign": "N0GEO",
        "applicant_type": "Individual",
        "first_name": "GEO", "middle_initial": "", "last_name": "NONE",
        "name_suffix": "",
        "entity_name": "",
        "operator_class": "T", "previous_operator_class": "",
        "previous_callsign": "",
        "trustee_callsign": "", "trustee_name": "",
        "street_address": "1 NOWHERE RD", "po_box": "",
        "city": "ANYTOWN", "state": "OR", "zip_code": "97201",
        "attention_line": "",
        "frn": "",
        "grant_date": "2010-01-01", "expired_date": "2030-01-01",
        "gridsquare": "",
        "coordinates": "",
        "county": "",
        "arrl_section": "",
        "dxcc_entity": "",
        "continent": "",
        "dxcc_id": None,
    },
]


def build_fixture(path):
    """Write the fixture sqlite at `path`. Returns the path.

    One file with both operator tables, mirroring production's single
    `lookup_data.sqlite`: the FCC and ISED adapters share one connection and
    differ only in which table they query. CA_FIXTURE is defined further down,
    next to the CA adapter's own checks; this runs at call time, not import.
    """
    conn = sqlite3.connect(path)
    for table, rows in (("fcc_operators", FCC_FIXTURE),
                        ("ca_operators", CA_FIXTURE)):
        conn.executescript(OPERATORS_SCHEMA.format(table=table))
        cols = list(rows[0].keys())
        placeholders = ",".join("?" for _ in cols)
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} ({','.join(cols)}) "
                f"VALUES ({placeholders})",
                [row[c] for c in cols],
            )
    conn.commit()
    conn.close()
    return path


# --- ultracheck fixture ----------------------------------------------------
# Mirror of the production ultracheck schema (data-parsers/ultracheck_README.md).
# Only what the reader touches: the `callsigns` table, the `call_suffix` index
# that makes a substring search a prefix range scan, and the `meta` row setup()
# reads to log the build. The real DB is 91 MB / 304k calls and gitignored, so
# the suite must never depend on it.
ULTRACHECK_SCHEMA = """
CREATE TABLE callsigns (
  id               INTEGER PRIMARY KEY,
  callsign         TEXT NOT NULL UNIQUE,
  fd_last_year     INTEGER,
  wfd_last_year    INTEGER,
  pota_hunter_qsos INTEGER,
  pota_activations INTEGER,
  lotw_last_upload TEXT,
  clublog          INTEGER NOT NULL DEFAULT 0,
  clublog_last_qso TEXT,
  scp              INTEGER NOT NULL DEFAULT 0,
  source_count     INTEGER NOT NULL DEFAULT 0,
  first_seen       TEXT,
  last_seen        TEXT
);
CREATE TABLE call_suffix (suffix TEXT NOT NULL, call_id INTEGER NOT NULL);
CREATE INDEX idx_suffix ON call_suffix(suffix, call_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Built so that every source's ordering is falsifiable on the single query
# "W1AW", where five rows match as substrings:
#   W1AW    the exact match, and deliberately the WORST row by every metric —
#           oldest years, fewest QSOs, oldest dates. If it still leads every
#           list, exact-first is genuinely beating each source's own sort.
#   W1AWX   the best row by every metric, so it leads whenever exact-first
#           isn't in play.
#   KW1AW   middle values, and matches mid-string rather than as a prefix.
#   W1AWZZ  older still, and the longest call — the SCP length sort's target.
#   W1AWQ   a Club Log member with a NULL last QSO (~60% of real ones are),
#           which must sort LAST in clublog rather than vanish. It is in no
#           other source, so it also proves each source filters to its own.
# K9XYZ matches nothing and must never appear.
ULTRACHECK_FIXTURE = [
    # callsign, fd,   wfd,  hunter, activ, lotw,         cl, cl_last_qso,           scp, src
    ("W1AW",    2010, 2010, 1,      1,     "2010-01-01", 1, "2010-01-01 00:00:00",  1,   7),
    ("W1AWX",   2025, 2025, 999,    99,    "2025-01-01", 1, "2025-01-01 00:00:00",  1,   6),
    ("KW1AW",   2020, 2020, 500,    50,    "2020-01-01", 1, "2020-01-01 00:00:00",  1,   7),
    ("W1AWZZ",  2015, 2015, 250,    25,    "2015-01-01", 1, "2015-01-01 00:00:00",  1,   6),
    ("W1AWQ",   None, None, None,   None,  None,         1, None,                   0,   1),
    # lotw-only rows, purely to push that one source over its limit of 5 so the
    # `truncated` flag has something to report while the others stay False.
    ("W1AWA",   None, None, None,   None,  "2011-01-01", 0, None,                   0,   1),
    ("W1AWB",   None, None, None,   None,  "2012-01-01", 0, None,                   0,   1),
    ("K9XYZ",   2025, 2025, 9999,   999,   "2026-01-01", 1, "2026-01-01 00:00:00",  1,   7),
]


def build_ultracheck_fixture(path):
    """Write the ultracheck fixture sqlite at `path`. Returns the path."""
    conn = sqlite3.connect(path)
    conn.executescript(ULTRACHECK_SCHEMA)
    for i, row in enumerate(ULTRACHECK_FIXTURE, start=1):
        conn.execute(
            "INSERT INTO callsigns (id, callsign, fd_last_year, wfd_last_year,"
            " pota_hunter_qsos, pota_activations, lotw_last_upload, clublog,"
            " clublog_last_qso, scp, source_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (i,) + row)
        # Every suffix of the call, exactly as the importer expands them: this
        # is what turns a substring match into an indexed prefix range scan.
        call = row[0]
        for start in range(len(call)):
            conn.execute("INSERT INTO call_suffix (suffix, call_id) VALUES (?,?)",
                         (call[start:], i))
    conn.execute("INSERT INTO meta (key, value) VALUES ('built_utc', ?)",
                 ("2026-07-29T00:00:00+00:00",))
    conn.execute("INSERT INTO meta (key, value) VALUES ('callsigns', ?)",
                 (str(len(ULTRACHECK_FIXTURE)),))
    conn.commit()
    conn.close()
    return path


# --- server helpers --------------------------------------------------------
def wait_for_server(proc):
    for _ in range(50):
        time.sleep(0.1)
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited early (code {proc.returncode})")
        try:
            urllib.request.urlopen(BASE + "/api/event", timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except (urllib.error.URLError, ConnectionError):
            continue
    raise AssertionError("server never came up")


def start_server(config_path):
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "main.py"),
                             str(config_path)])
    try:
        wait_for_server(proc)
    except Exception:
        stop_server(proc)
        raise
    return proc


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def cleanup(data_dir):
    for _ in range(20):
        shutil.rmtree(data_dir, ignore_errors=True)
        if not data_dir.exists():
            return
        time.sleep(0.3)
    print(f"warning: could not remove {data_dir}")


def preclean():
    """Remove any leftover scratch dirs from prior failed runs (Windows can
    hold a transient lock on them for a moment after a hard kill)."""
    base = Path(tempfile.gettempdir())
    for d in base.glob("haml-lookup-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


async def post_lookup(session, callsign):
    async with session.post(BASE + "/api/lookup",
                            json={"callsign": callsign},
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
        text = await resp.text()
        try:
            body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = {"_raw": text}
        return resp.status, body


async def post_raw(session, body):
    """POST /api/lookup with a non-dict body — used to assert the bad-input
    400 path without going through the dict-shaped post_lookup helper."""
    async with session.post(BASE + "/api/lookup",
                            data=body,
                            headers={"Content-Type": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=5)) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text) if text else {}
        except json.JSONDecodeError:
            return resp.status, {"_raw": text}


# --- unit checks (no server) ----------------------------------------------
def check_ttl_policy():
    """Verify the TTL policy constants haven't drifted."""
    check(
        lookup_cache.TTL_OK == 365 * 24 * 60 * 60,
        f"TTL_OK == {365 * 24 * 60 * 60} (365 days)",
    )
    expected_month = 30 * 24 * 60 * 60
    check(
        lookup_cache.TTL_NOT_FOUND == expected_month,
        f"TTL_NOT_FOUND == {expected_month} (1 month)",
    )
    check(
        lookup_cache.TTL_ERROR == 15 * 60,
        f"TTL_ERROR == {15 * 60} (15 min)",
    )

    from datetime import datetime, timezone as _tz
    def lifetime_seconds(status, dirty=False):
        s = lookup_cache._expires_at(status, dirty=dirty)
        check(s != "", f"_expires_at({status!r}, dirty={dirty}) returns non-empty")
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        now = datetime.now(_tz.utc)
        return (dt - now).total_seconds()

    ok_clean = lifetime_seconds(lookup_cache.STATUS_OK, dirty=False)
    check(
        abs(ok_clean - 365 * 24 * 60 * 60) < 5,
        f"ok-clean lifetime ~365 days (got {ok_clean:.0f}s)",
    )

    ok_dirty = lifetime_seconds(lookup_cache.STATUS_OK, dirty=True)
    check(
        abs(ok_dirty - 15 * 60) < 5,
        f"ok-dirty lifetime ~15 min (got {ok_dirty:.0f}s)",
    )

    nf_secs = lifetime_seconds(lookup_cache.STATUS_NOT_FOUND)
    check(
        abs(nf_secs - expected_month) < 5,
        f"not_found lifetime ~30 days (got {nf_secs:.0f}s, expected {expected_month}s)",
    )

    err_secs = lifetime_seconds(lookup_cache.STATUS_ERROR)
    check(
        abs(err_secs - 15 * 60) < 5,
        f"error lifetime ~15 min (got {err_secs:.0f}s, expected {15 * 60}s)",
    )

    check(
        lookup_cache.now_iso is lookup_record.now_iso,
        "lookup_cache.now_iso re-exports lookup_record.now_iso",
    )


def check_coerce():
    """Verify lookup_record.coerce() shapes input into the canonical record,
    and that the ISO date coercer accepts YYYY-MM-DD in addition to MM/DD/YYYY
    (FCC ULS stores dates in ISO form)."""
    full_input = {
        "callsign": "W1AW",
        "name": "JOSHUA D VILLWOCK",
        "license_type": "PERSON",
        "license_class": "EXTRA",
        "previous_callsign": "KG7WKU",
        "previous_license_class": "GENERAL",
        "trustee_callsign": "",
        "trustee_name": "",
        "address_line1": "14970 SALT CREEK RD",
        "address_line2": "DALLAS, OR 97338",
        "address_attn": "",
        "state": "Oregon",   # spelled-out on purpose — must map to the code
        "county": "",
        "section": "or",     # lowercase on purpose — must come back uppercased
        "country": "",
        "continent": "",
        "latitude": "44.979441",
        "longitude": "-123.337862",
        "gridsquare": "CN84hx",
        "frn": "0024933376",
        "grant_date": "2024-03-19",  # ISO form — used to be a dirty field
        "expiry_date": "2034-03-19",
        "dxcc": "291",  # numeric string from upstream; coercer -> 291
        "fetched_at": "2026-07-16T20:11:04.123+00:00",
        "source": "fcc",
        "junk": "ignore me",
    }
    record, bad = lookup_record.coerce(full_input)
    check(set(record.keys()) == set(lookup_record.FIELDS),
          "full fixture -> output keys == FIELDS exactly")
    check(bad == [], f"full fixture -> no bad_fields (got {bad})")
    check(record["callsign"] == "W1AW", "full fixture -> callsign")
    check(record["license_type"] == "person",
          "full fixture -> license_type lowercased")
    check(record["license_class"] == "extra",
          "full fixture -> license_class lowercased")
    check(record["previous_license_class"] == "general",
          "full fixture -> previous_license_class lowercased")
    check(record["trustee_callsign"] is None,
          "full fixture -> trustee_callsign None (empty)")
    check(isinstance(record["latitude"], float) and record["latitude"] == 44.979441,
          "full fixture -> latitude is float")
    check(isinstance(record["longitude"], float) and record["longitude"] == -123.337862,
          "full fixture -> longitude is float")
    check(record["grant_date"] == "2024-03-19",
          "full fixture -> ISO grant_date preserved")
    check(record["expiry_date"] == "2034-03-19",
          "full fixture -> ISO expiry_date preserved")
    check(record["frn"] == "0024933376", "full fixture -> frn preserved")
    check(record["gridsquare"] == "CN84",
          f"full fixture -> gridsquare truncated to 4 chars "
          f"(got {record['gridsquare']!r})")
    check(record["dxcc"] == 291,
          f"full fixture -> dxcc numeric string coerced to int "
          f"(got {record['dxcc']!r})")

    # Lowercase input must be uppercased and accepted as clean.
    lower_input = {**full_input, "gridsquare": "cn84mo"}
    lower_record, lower_bad = lookup_record.coerce(lower_input)
    check(lower_record["gridsquare"] == "CN84",
          f"lowercase gridsquare -> 'CN84' (got {lower_record['gridsquare']!r})")
    check(lower_bad == [],
          f"lowercase gridsquare -> no bad_fields (got {lower_bad})")

    # Junk that truncates but doesn't match the Maidenhead pattern must be
    # flagged dirty exactly like an unparseable date or latitude.
    junk_input = {**full_input, "gridsquare": "9xq"}
    junk_record, junk_bad = lookup_record.coerce(junk_input)
    check(junk_record["gridsquare"] is None,
          f"junk gridsquare -> None (got {junk_record['gridsquare']!r})")
    check("gridsquare" in junk_bad,
          f"junk gridsquare -> 'gridsquare' in bad_fields (got {junk_bad})")

    check("junk" not in record, "full fixture -> unknown key dropped")

    # State: spelled-out name maps to the USPS code; blank county/country
    # coerce to a clean null.
    check(record["state"] == "OR",
          f"full fixture -> 'Oregon' maps to 'OR' (got {record['state']!r})")
    check(record["county"] is None, "full fixture -> blank county is None")
    check(record["section"] == "OR",
          f"full fixture -> section uppercased (got {record['section']!r})")
    check(record["country"] is None, "full fixture -> blank country is None")
    check(record["continent"] is None, "full fixture -> blank continent is None")

    # A two-letter code (any case) passes through uppercased and clean.
    code_record, code_bad = lookup_record.coerce({**full_input, "state": "or"})
    check(code_record["state"] == "OR",
          f"lowercase 'or' -> 'OR' (got {code_record['state']!r})")
    check(code_bad == [], f"lowercase 'or' -> no bad_fields (got {code_bad})")

    # An unrecognized state is present-but-uncoercible -> dirty.
    junk_state_record, junk_state_bad = lookup_record.coerce(
        {**full_input, "state": "OREGONIA"})
    check(junk_state_record["state"] is None,
          f"junk state -> None (got {junk_state_record['state']!r})")
    check("state" in junk_state_bad,
          f"junk state -> 'state' in bad_fields (got {junk_state_bad})")

    # Sparse: only license_type and name provided. Everything else is null,
    # and dirty must be False (sparse data is not a coercion failure).
    sparse_input = {"license_type": "CLUB", "name": "ARRL HQ"}
    record, bad = lookup_record.coerce(sparse_input)
    check(set(record.keys()) == set(lookup_record.FIELDS),
          "sparse fixture -> output keys == FIELDS exactly")
    check(bad == [], f"sparse fixture -> no bad_fields (got {bad})")
    check(record["license_type"] == "club", "sparse fixture -> license_type")
    check(record["name"] == "ARRL HQ", "sparse fixture -> name")
    check(record["callsign"] is None, "sparse fixture -> callsign is None")
    check(record["latitude"] is None, "sparse fixture -> latitude is None")
    check(record["grant_date"] is None, "sparse fixture -> grant_date is None")

    # Garbage: present-but-uncoercible values. These must become None AND
    # be reported in bad_fields so the cache layer can shorten the TTL.
    garbage_input = {
        "callsign": "TEST",
        "license_type": "CLUB",
        "latitude": "abc",          # bad float
        "longitude": "",            # empty -> clean None
        "grant_date": "not a date", # bad date
        "dxcc": 99999,              # out of range -> dirty
    }
    record, bad = lookup_record.coerce(garbage_input)
    check(set(record.keys()) == set(lookup_record.FIELDS),
          "garbage fixture -> output keys == FIELDS exactly")
    check(record["latitude"] is None, "garbage fixture -> latitude is None")
    check(record["longitude"] is None, "garbage fixture -> longitude is None")
    check(record["grant_date"] is None, "garbage fixture -> grant_date is None")
    check(record["dxcc"] is None, "garbage fixture -> dxcc is None")
    check(set(bad) == {"latitude", "grant_date", "dxcc"},
          f"garbage fixture -> bad_fields == {{latitude, grant_date, dxcc}} (got {bad})")

    # Backwards compat: legacy Callook-style MM/DD/YYYY dates must still
    # coerce to YYYY-MM-DD — a Callook row in the cache must read back
    # cleanly through the new coercer.
    legacy_input = {
        "callsign": "K1MI",
        "grant_date": "03/19/2024",
        "expiry_date": "03/19/2034",
    }
    legacy, legacy_bad = lookup_record.coerce(legacy_input)
    check(legacy["grant_date"] == "2024-03-19",
          f"legacy MM/DD/YYYY grant_date -> 2024-03-19 (got {legacy['grant_date']!r})")
    check(legacy["expiry_date"] == "2034-03-19",
          f"legacy MM/DD/YYYY expiry_date -> 2034-03-19 (got {legacy['expiry_date']!r})")
    check(legacy_bad == [],
          f"legacy MM/DD/YYYY dates -> no bad_fields (got {legacy_bad})")


def check_postprocess_unit():
    """Verify lookup_postprocess.apply() is the single out-bound stage:
    it derives CQ/ITU zones from coordinates (only-fill-if-null, so a source
    that already knows its zones wins) and stamps a request-time distance
    from the active event's operating position. Never mutates the input."""
    import lookup_postprocess
    record = {"callsign": "W1AW", "latitude": 44.979441, "longitude": -123.337862}
    loc_app = {"event": {"config": {
        "location": {"latitude": 45.5152, "longitude": -122.6784}}}}

    # ---- distance ----
    out = lookup_postprocess.apply(loc_app, record)
    check(out["distance"] == 78,
          f"Portland -> Dallas OR == 78 km floored (got {out['distance']!r})")
    check("distance" not in record,
          "apply() leaves the input record unmodified")

    out = lookup_postprocess.apply(loc_app, {"latitude": None, "longitude": None})
    check(out["distance"] is None, "no record coords -> distance is None")

    # No operating position to measure from falls back to _DEFAULT_LOCATION
    # (45, -123) rather than declining to answer.
    out = lookup_postprocess.apply({"event": {"config": {"location": None}}},
                                   record)
    check(out["distance"] == 26,
          f"no event location -> distance from default (got {out['distance']!r})")

    out = lookup_postprocess.apply({}, record)
    check(out["distance"] == 26,
          f"no active event -> distance from default (got {out['distance']!r})")

    # ---- zone derivation (moved here out of the FCC adapter) ----
    # Dallas, OR is CQ 3, ITU 6. The FCC adapter now hands over a record with
    # coords and null zones; this stage is what fills them.
    out = lookup_postprocess.apply({}, dict(record, cq_zone=None, itu_zone=None))
    check(out["cq_zone"] == 3,
          f"coords -> cq_zone 3 (Dallas, OR; got {out['cq_zone']!r})")
    check(out["itu_zone"] == 6,
          f"coords -> itu_zone 6 (Dallas, OR; got {out['itu_zone']!r})")

    # Only-fill-if-null: CallParser's prefix-DB zones must survive.
    out = lookup_postprocess.apply({}, dict(record, cq_zone=14, itu_zone=27))
    check(out["cq_zone"] == 14 and out["itu_zone"] == 27,
          f"source-supplied zones win over derivation "
          f"(got {out['cq_zone']!r}/{out['itu_zone']!r})")

    # No coords -> no zones, no crash.
    out = lookup_postprocess.apply(
        {}, {"latitude": None, "longitude": None, "cq_zone": None,
             "itu_zone": None})
    check(out["cq_zone"] is None and out["itu_zone"] is None,
          "no coords -> zones stay None")

    # ---- found ----
    # Defaults True: every caller that has a record to hand over has a hit,
    # so a source's OK never has to say so. Only the miss path passes False.
    out = lookup_postprocess.apply({}, record)
    check(out["found"] is True, "apply() stamps found=true by default")
    out = lookup_postprocess.apply({}, record, found=False)
    check(out["found"] is False, "apply(found=False) stamps found=false")
    check("found" not in record, "found is not written back into the input")

    # An all-null record through apply() is the miss shape api_rest returns:
    # every canonical field null, `found` false, extras present. Asserted here
    # too so a break shows up as a unit failure, not only as an e2e one.
    blank, _ = lookup_record.coerce({"callsign": "ZZZZZZ"})
    out = lookup_postprocess.apply({}, blank, found=False)
    check(out["found"] is False and out["distance"] is None
          and out["pota_park"] is None,
          "blank record -> found false, distance and pota_park null")
    leaked = {f: out[f] for f in lookup_record.FIELDS
              if f != "callsign" and out.get(f) is not None}
    check(not leaked, f"blank record derives nothing (got {leaked})")


def check_ultracheck_unit(uc_fixture_path):
    """Verify lookup_ultracheck against its fixture: the config limits are
    honored per source, the search is a true substring match, and the module
    degrades instead of raising. Ordering itself is asserted e2e, where it is
    the wire shape that matters."""
    import lookup_ultracheck

    check(set(lookup_ultracheck.SOURCE_LIMITS) == set(_UC_SOURCES),
          f"every source has a configured limit "
          f"(got {sorted(lookup_ultracheck.SOURCE_LIMITS)})")

    # ---- degrade paths: no DB at all, and a term with nothing to search ----
    # Both must answer the full shape rather than raising or returning None,
    # because postprocess writes whatever comes back straight onto the response.
    out = lookup_ultracheck.search({}, "W1AW")
    check(out["available"] is False and set(out["sources"]) == set(_UC_SOURCES),
          "no DB in the app -> available false, all sources present")

    app = {"cfg": {"ultracheck_db_path": uc_fixture_path}}
    lookup_ultracheck.setup(app)
    check(app.get("ultracheck_db") is not None,
          "setup() opens the fixture")
    try:
        for term in ("", None, "   "):
            out = lookup_ultracheck.search(app, term)
            check(out["available"] is True
                  and all(not s["matches"] for s in out["sources"].values()),
                  f"empty term {term!r} -> no matches, but still available")

        # ---- substring, not prefix ----
        # 'KW1AW' contains 'W1AW' in the middle; a prefix-only search misses it.
        out = lookup_ultracheck.search(app, "W1AW")
        check("KW1AW" in [m["callsign"]
                          for m in out["sources"]["fd"]["matches"]],
              "mid-string match is found (substring, not prefix)")
        # Lowercase in, uppercase match out: callsigns are stored uppercase.
        lower = lookup_ultracheck.search(app, "w1aw")
        check(lower["query"] == "W1AW"
              and [m["callsign"] for m in lower["sources"]["fd"]["matches"]]
                  == [m["callsign"] for m in out["sources"]["fd"]["matches"]],
              "a lowercase term is uppercased before searching")

        # ---- a source's filter is its own ----
        # W1AWQ is a Club Log member and nothing else, so it appears in clublog
        # and in no other list. A NULL means "this source never heard of the
        # call", so a source must never claim a row it doesn't own.
        for name in _UC_SOURCES:
            calls = [m["callsign"] for m in out["sources"][name]["matches"]]
            check(("W1AWQ" in calls) == (name == "clublog"),
                  f"{name}: W1AWQ appears only under clublog (got {calls})")

        # ---- the limit is the limit ----
        # Temporarily squeeze every limit to 1: exactly one match per source
        # that has any, and truncated wherever more existed.
        original = dict(lookup_ultracheck.SOURCE_LIMITS)
        try:
            for name in lookup_ultracheck.SOURCE_LIMITS:
                lookup_ultracheck.SOURCE_LIMITS[name] = 1
            squeezed = lookup_ultracheck.search(app, "W1AW")
            check(all(len(s["matches"]) == 1
                      for s in squeezed["sources"].values()),
                  "limit 1 -> exactly one match per source")
            check(all(s["truncated"] for s in squeezed["sources"].values()),
                  "limit 1 with more available -> truncated everywhere")
            check(all(s["matches"][0]["callsign"] == "W1AW"
                      for s in squeezed["sources"].values()),
                  "limit 1 -> the one match kept is the exact one")
            # A limit of 0 must return nothing without running a query.
            for name in lookup_ultracheck.SOURCE_LIMITS:
                lookup_ultracheck.SOURCE_LIMITS[name] = 0
            zeroed = lookup_ultracheck.search(app, "W1AW")
            check(all(not s["matches"] and not s["truncated"]
                      for s in zeroed["sources"].values()),
                  "limit 0 -> no matches, nothing flagged")
        finally:
            lookup_ultracheck.SOURCE_LIMITS.clear()
            lookup_ultracheck.SOURCE_LIMITS.update(original)

        # ---- a term matching nothing ----
        out = lookup_ultracheck.search(app, "QQQQQQ")
        check(out["available"] is True
              and all(not s["matches"] for s in out["sources"].values()),
              "no matches -> available true with empty lists (not unavailable)")
    finally:
        lookup_ultracheck.close(app)
    check(app.get("ultracheck_db") is None, "close() releases the handle")

    # ---- a missing file is a warning, not an exception ----
    app2 = {"cfg": {"ultracheck_db_path": "C:/nonexistent/ultracheck.sqlite"}}
    lookup_ultracheck.setup(app2)
    check(app2.get("ultracheck_db") is None,
          "setup() with a missing file -> None, no raise")


def check_location_calc_unit():
    """Verify lookup_location_calc's derivations against the shipped dataset.

    Two kinds of assertion here, deliberately. Polygon-derived answers
    (zones, DXCC, county) are checked by value: boundary datasets are stable
    between rebuilds. Anchors are not — `process_section` and `process_state`
    average a licensee population that changes with every ULS dump — so
    those are checked by property: which section the anchor lands in, not
    what its digits are.
    """
    import lookup_location_calc as loc

    LAT, LON = 41.7148, -72.7273      # W1AW, Newington CT

    # ---- recalculate(): every registry field from one coordinate ----
    rec = loc.recalculate({"latitude": LAT, "longitude": LON})
    check(rec["cq_zone"] == 5 and rec["itu_zone"] == 8,
          f"W1AW coords -> CQ 5 / ITU 8 (got {rec['cq_zone']!r}/{rec['itu_zone']!r})")
    check(rec["dxcc"] == 291 and rec["country"] == "United States of America",
          f"W1AW coords -> DXCC 291 (got {rec['dxcc']!r}/{rec['country']!r})")
    check(rec["state"] == "CT" and rec["section"] == "CT",
          f"W1AW coords -> state/section CT (got {rec['state']!r}/{rec['section']!r})")
    # Connecticut has planning regions, not counties: Hartford does not exist.
    check(rec["county"] == "Capitol",
          f"W1AW coords -> county Capitol (got {rec['county']!r})")
    check(rec["gridsquare"] == "FN31",
          f"W1AW coords -> gridsquare FN31 (got {rec['gridsquare']!r})")

    # A subset writes only what it names, so a caller can refresh one field
    # without disturbing the rest of the record.
    rec = loc.recalculate({"latitude": LAT, "longitude": LON,
                           "state": "XX", "county": "Nowhere"}, ["state"])
    check(rec["state"] == "CT" and rec["county"] == "Nowhere",
          f"subset rewrites only the named field (got {rec['state']!r}/{rec['county']!r})")

    # An unrecognized name is skipped; the rest of the list still applies.
    rec = loc.recalculate({"latitude": LAT, "longitude": LON},
                          ["state", "nonesuch"])
    check(rec["state"] == "CT" and "nonesuch" not in rec,
          "an unknown field name is skipped, the rest still apply")

    # No coordinates: nothing to derive from, so nothing is touched.
    rec = loc.recalculate({"state": "XX"})
    check(rec == {"state": "XX"},
          f"no coordinates -> record untouched (got {rec!r})")

    # A point outside every polygon nulls the named fields rather than
    # leaving values that describe somewhere the operator no longer is.
    rec = loc.recalculate({"latitude": 40.0, "longitude": -40.0,
                           "state": "CT", "county": "Capitol", "dxcc": 291})
    check(rec["state"] is None and rec["county"] is None and rec["dxcc"] is None,
          "mid-Atlantic -> the named fields are nulled, not left stale")

    # ---- gridsquare, both directions ----
    check(loc.derive_gridsquare(LAT, LON)["gridsquare"] == "FN31",
          "derive_gridsquare on W1AW -> FN31")
    check(loc.derive_gridsquare(0, 180) == loc.derive_gridsquare(0, -180),
          "+180 and -180 are the same meridian and the same square")
    rec = loc.process_gridsquare({}, "fn31ab")
    check(rec["gridsquare"] == "FN31",
          f"a long lowercase locator is stored as the 4-char form (got {rec['gridsquare']!r})")
    check(loc.derive_gridsquare(rec["latitude"], rec["longitude"])["gridsquare"] == "FN31",
          "grid -> centre -> grid round-trips to the same square")
    stale = {"gridsquare": "FN31", "latitude": 1.0, "longitude": 2.0}
    check(loc.process_gridsquare(stale, "ZZ99") is None
          and stale == {"gridsquare": "FN31", "latitude": 1.0, "longitude": 2.0},
          "an unusable locator answers None and writes nothing")

    # ---- POTA park ----
    rec = loc.process_park({}, {"their_park": "us-0001, US-0002"})
    check(rec is not None
          and loc.derive_county(rec["latitude"], rec["longitude"])["state"] == "ME",
          "first reference of a multi-park list wins (US-0001 is in Maine)")
    check(bool(rec.get("pota_park")),
          f"a matched park sets pota_park to its name (got {rec.get('pota_park')!r})")
    stale = {"latitude": 1.0, "longitude": 2.0}
    check(loc.process_park(stale, {"their_park": "ZZ-9999"}) is None
          and stale == {"latitude": 1.0, "longitude": 2.0},
          "an unknown reference answers None and writes nothing")
    check(loc.process_park({}, {}) is None and loc.process_park({}, None) is None,
          "no park typed answers None")
    # POTA's (0,0) placeholder means "coordinates unknown", not the Gulf of
    # Guinea, so a park carrying it must not move the record.
    conn = loc._conn()
    row = conn.execute("SELECT reference FROM pota_parks "
                       "WHERE latitude = 0 AND longitude = 0 LIMIT 1").fetchone()
    if row:
        stale = {"latitude": 1.0, "longitude": 2.0}
        check(loc.process_park(stale, {"their_park": row[0]}) is None
              and stale == {"latitude": 1.0, "longitude": 2.0},
              f"a park at POTA's (0,0) placeholder is refused ({row[0]})")

    # ---- section / state anchors ----
    rec = loc.process_section({}, "ct")
    check(rec["section"] == "CT",
          f"process_section writes the coerced section (got {rec['section']!r})")
    check(loc.derive_county(rec["latitude"], rec["longitude"])["section"] == "CT",
          "the CT anchor lands inside the CT section")
    # The anchor is a licensee's own position, not the mean of the population.
    # An unsnapped mean sits in whatever lake or stretch of water the average
    # lands in, and every field derived from it answers for that instead.
    hit = conn.execute(
        f"SELECT 1 FROM fcc_operators WHERE arrl_section = 'CT' "
        f"AND coordinates IS NOT NULL "
        f"AND abs({loc._OP_LAT} - ?) < 1e-9 AND abs({loc._OP_LON} - ?) < 1e-9 "
        f"LIMIT 1", (rec["latitude"], rec["longitude"])).fetchone()
    check(hit is not None,
          "the anchor is a real licensee's position, not the raw mean")
    # PAC spans the antimeridian: Hawaii near -157, Guam near +145. Averaging
    # degrees puts the anchor in California, which is the bug this guards.
    rec = loc.process_section({}, "PAC")
    check(abs(rec["longitude"]) >= 140,
          f"the PAC anchor is in the Pacific, not on the mainland "
          f"(got longitude {rec['longitude']!r})")
    rec = loc.process_state({}, "connecticut")
    check(rec["state"] == "CT"
          and loc.derive_county(rec["latitude"], rec["longitude"])["state"] == "CT",
          "a spelled-out state anchors in that state and stores the code")
    # Territories the record's own state coercer rejects still anchor, because
    # the licensee table carries them.
    rec = loc.process_state({}, "PR")
    check(rec is not None and rec["state"] == "PR",
          f"a US territory code anchors (got {rec and rec['state']!r})")
    stale = {"section": "CT", "latitude": 1.0, "longitude": 2.0}
    check(loc.process_section(stale, "ZZZ") is None
          and stale == {"section": "CT", "latitude": 1.0, "longitude": 2.0},
          "a section naming no licensee answers None and writes nothing")

    # ---- section from state ----
    check(loc.recalculate_section_from_state({"state": "CT"})["section"] == "CT",
          "a single-section state fills its section")
    check(loc.recalculate_section_from_state({"state": "DC"})["section"] == "MDC",
          "a section is not the state's own code (DC -> MDC)")
    rec = loc.recalculate_section_from_state({"state": "CA", "section": "LAX"})
    check(rec["section"] == "LAX",
          f"a split state leaves the existing section alone (got {rec['section']!r})")
    rec = loc.recalculate_section_from_state({"state": "ZZ", "section": "OR"})
    check(rec["section"] == "OR",
          "an unrecognized state leaves the existing section alone")

    # ---- blank() ----
    rec = lookup_record.blank(
        {"state": "CT", "county": "Capitol", "section": "CT"},
        ["county", "section"])
    check(rec["state"] == "CT" and rec["county"] is None and rec["section"] is None,
          "blank() nulls only the named fields")
    rec = {"state": "CT"}
    check(lookup_record.blank(rec, ["nonesuch"]) is rec and rec["state"] == "CT",
          "blank() skips an unknown field name and returns the same record")


def check_override_unit():
    """Verify the operator-override chain in lookup_postprocess.apply().

    The operator's own typing outranks whatever a source said, most precise
    source first: coordinates, then a POTA park, then section, state and
    gridsquare. Each branch establishes a position, re-derives what that
    position supports, and blanks what it cannot.

    A branch that cannot resolve must leave the record exactly as it found
    it and let a coarser override have its turn — that atomicity is what the
    regression checks at the end are for.
    """
    import lookup_postprocess

    app = {"event": {"config": {
        "location": {"latitude": 45.5152, "longitude": -122.6784}}}}  # Portland OR
    base = {"callsign": "W1AW", "latitude": 41.7148, "longitude": -72.7273,
            "gridsquare": "FN31", "state": "CT", "county": "Capitol",
            "section": "CT", "country": "United States of America",
            "dxcc": 291, "cq_zone": 5, "itu_zone": 8}
    baseline = lookup_postprocess.apply(app, dict(base))["distance"]

    # ---- typed coordinates: trusted outright, everything re-derived ----
    out = lookup_postprocess.apply(app, dict(base),
                                   {"latitude": "21.3", "longitude": "-157.8"})
    check(out["state"] == "HI" and out["dxcc"] == 110 and out["cq_zone"] == 31,
          f"typed coordinates re-derive every field "
          f"(got {out['state']!r}/{out['dxcc']!r}/{out['cq_zone']!r})")
    check(out["distance"] is not None and out["distance"] != baseline,
          "distance is recomputed from the overridden position")

    # ---- POTA park ----
    out = lookup_postprocess.apply(app, dict(base), {"their_park": "US-0001"})
    check(out["state"] == "ME" and out["county"] == "Hancock",
          f"a park moves the record onto it (got {out['state']!r}/{out['county']!r})")
    check(bool(out.get("pota_park")), "the park's name reaches the wire shape")
    # The request-time extras are always present, null when they have nothing
    # to say, so the client never has to test for a missing key.
    plain = lookup_postprocess.apply(app, dict(base))
    check("pota_park" in plain and plain["pota_park"] is None
          and "distance" in plain,
          "the wire shape always carries pota_park and distance, null when unset")

    # ---- section: derives its own state, blanks what it can't support ----
    out = lookup_postprocess.apply(app, dict(base), {"section": "NTX"})
    check(out["section"] == "NTX" and out["state"] == "TX",
          f"a section override derives its own state (got {out['section']!r}/{out['state']!r})")
    check(out["gridsquare"] is None and out["county"] is None,
          "a section cannot support a gridsquare or county, so both are blanked")

    # ---- state: section comes back only when the state names one ----
    out = lookup_postprocess.apply(app, dict(base), {"state": "HI"})
    check(out["state"] == "HI" and out["section"] == "PAC",
          f"a single-section state re-fills its section (got {out['section']!r})")
    out = lookup_postprocess.apply(app, dict(base), {"state": "TX"})
    check(out["section"] is None,
          f"a split state leaves the section blank (got {out['section']!r})")

    # ---- gridsquare ----
    out = lookup_postprocess.apply(app, dict(base), {"gridsquare": "BL11"})
    check(out["gridsquare"] == "BL11" and out["cq_zone"] == 31,
          f"a gridsquare override re-derives from the square's centre "
          f"(got {out['gridsquare']!r}/{out['cq_zone']!r})")
    check(out["state"] is None and out["section"] is None and out["county"] is None,
          "a 4-char square is too coarse for state/section/county, so they are blanked")

    # ---- precedence: most precise source wins, and only one branch runs ----
    out = lookup_postprocess.apply(app, dict(base), {
        "latitude": "21.3", "longitude": "-157.8", "their_park": "US-0001",
        "section": "NTX", "state": "TX", "gridsquare": "BL11"})
    check(out["state"] == "HI" and out["pota_park"] is None,
          f"typed coordinates outrank every coarser override (got {out['state']!r})")
    out = lookup_postprocess.apply(app, dict(base),
                                   {"their_park": "US-0001", "section": "NTX"})
    check(out["state"] == "ME", f"a park outranks a section (got {out['state']!r})")
    out = lookup_postprocess.apply(app, dict(base),
                                   {"section": "NTX", "state": "HI"})
    check(out["state"] == "TX", f"a section outranks a state (got {out['state']!r})")

    # ---- a value equal to the record's is not an override ----
    out = lookup_postprocess.apply(app, dict(base), {"state": "CT"})
    check(out["county"] == "Capitol" and out["gridsquare"] == "FN31",
          "typing what the lookup already said changes nothing")

    # ---- regressions: a branch that cannot resolve must do nothing ----
    out = lookup_postprocess.apply(app, dict(base), {"section": "ZZZ"})
    check(out["section"] == "CT" and out["gridsquare"] == "FN31"
          and out["county"] == "Capitol",
          "a section that names no licensee leaves the record untouched")
    out = lookup_postprocess.apply(app, dict(base),
                                   {"section": "ZZZ", "state": "HI"})
    check(out["state"] == "HI",
          "an unresolvable section falls through to the state the operator also typed")
    # `entry` carries fields the operator emptied, so an override that reads
    # as absent must not consume the chain either.
    out = lookup_postprocess.apply(app, dict(base),
                                   {"latitude": "", "longitude": "", "state": "HI"})
    check(out["state"] == "HI",
          "emptied coordinates fall through to a coarser override")
    out = lookup_postprocess.apply(app, dict(base),
                                   {"latitude": "21.3", "state": "HI"})
    check(out["state"] == "HI",
          "one coordinate alone is not a position, so it falls through")

    # ---- the input record is never mutated ----
    src = dict(base)
    lookup_postprocess.apply(app, src, {"their_park": "US-0001"})
    check(src == base, "apply() never mutates the record it was handed")


def check_chain_unit():
    """Verify the source chain's shape and the caching contract.

    The chain is an ordered tuple of modules; order is priority and each
    module declares whether its OK results may be persisted. Nothing shipped
    is cacheable, so no lookup should ever write a cache row.
    """
    import lookup
    import lookup_ca
    import lookup_callparser
    import lookup_fcc

    check(lookup.SOURCES == (lookup_fcc, lookup_ca, lookup_callparser),
          f"SOURCES order is fcc -> ised -> callparser "
          f"(got {[s.SOURCE for s in lookup.SOURCES]})")
    check([s.CACHED for s in lookup.SOURCES] == [False, False, False],
          f"CACHED flags are all False "
          f"(got {[s.CACHED for s in lookup.SOURCES]})")
    for source in lookup.SOURCES:
        check(callable(getattr(source, "setup", None)),
              f"{source.SOURCE} exposes setup()")
        check(callable(getattr(source, "lookup", None)),
              f"{source.SOURCE} exposes lookup()")


async def check_chain_fallthrough_unit():
    """Drive lookup._run_lookup with stub sources to lock in the
    fall-through rules: first OK wins, misses and errors both advance, the
    FIRST error is what surfaces when nothing resolves, and only a CACHED
    source's OK gets written to the cache."""
    import lookup

    def _stub(name, status, cached=False, error="", payload=None):
        mod = type(sys)(f"stub_{name}")
        mod.SOURCE = name
        mod.CACHED = cached
        mod.setup = lambda app: None
        mod.lookup = lambda app, callsign, _s=status, _e=error, _p=payload: {
            "status": _s, "payload": dict(_p or {}, callsign=callsign),
            "error": _e}
        return mod

    scratch = Path(tempfile.mkdtemp(prefix="haml-chain-unit-"))
    original = lookup.SOURCES
    try:
        app = {"lookup_cache": lookup_cache.open_cache(scratch / "cache.db")}

        # ---- first OK wins; a later source never runs ----
        hit = _stub("hit", lookup_cache.STATUS_OK)
        never = _stub("never", lookup_cache.STATUS_OK, payload={"name": "NO"})
        lookup.SOURCES = (_stub("miss", lookup_cache.STATUS_NOT_FOUND),
                          hit, never)
        result = await lookup._run_lookup(app, "W1AW")
        check(result["status"] == lookup_cache.STATUS_OK,
              "chain: miss -> OK yields OK")
        check(result["payload"].get("name") is None,
              "chain: the source after the first OK never runs")
        check(lookup_cache.stats(app["lookup_cache"])[lookup_cache.STATUS_OK] == 0,
              "chain: an OK from a non-caching source writes no cache row")

        # ---- a CACHED source's OK is persisted by the dispatcher ----
        lookup.SOURCES = (_stub("cachehit", lookup_cache.STATUS_OK,
                                cached=True),)
        await lookup._run_lookup(app, "K1MI")
        row = lookup_cache.get(app["lookup_cache"], "K1MI")
        check(row is not None and row["source"] == "cachehit",
              f"chain: a CACHED source's OK is written (got {row and row['source']!r})")

        # ---- error falls through; a later OK still wins ----
        lookup.SOURCES = (_stub("broken", lookup_cache.STATUS_ERROR,
                                error="dataset unavailable"),
                          _stub("rescue", lookup_cache.STATUS_OK))
        result = await lookup._run_lookup(app, "G4ABC")
        check(result["status"] == lookup_cache.STATUS_OK,
              "chain: an erroring source doesn't abort the chain")

        # ---- all miss, one errored -> the FIRST error surfaces (502) ----
        lookup.SOURCES = (_stub("broken", lookup_cache.STATUS_ERROR,
                                error="dataset unavailable"),
                          _stub("alsobroken", lookup_cache.STATUS_ERROR,
                                error="second error"),
                          _stub("miss", lookup_cache.STATUS_NOT_FOUND))
        result = await lookup._run_lookup(app, "ZZZZZZ")
        check(result["status"] == lookup_cache.STATUS_ERROR,
              "chain: all-miss-with-an-error -> ERROR (502)")
        check(result["error"] == "dataset unavailable",
              f"chain: the FIRST error surfaces (got {result['error']!r})")

        # ---- all miss, none errored -> NOT_FOUND (a 200, found:false) ----
        lookup.SOURCES = (_stub("a", lookup_cache.STATUS_NOT_FOUND),
                          _stub("b", lookup_cache.STATUS_NOT_FOUND))
        result = await lookup._run_lookup(app, "ZZZZZZ")
        check(result["status"] == lookup_cache.STATUS_NOT_FOUND,
              "chain: all-miss-no-error -> NOT_FOUND")

        # ---- a source that raises presents as ERROR, chain continues ----
        boom = type(sys)("stub_boom")
        boom.SOURCE = "boom"
        boom.CACHED = False
        boom.setup = lambda app: None
        def _raise(app, callsign):
            raise RuntimeError("kaboom")
        boom.lookup = _raise
        lookup.SOURCES = (boom, _stub("rescue", lookup_cache.STATUS_OK))
        result = await lookup._run_lookup(app, "W1AW")
        check(result["status"] == lookup_cache.STATUS_OK,
              "chain: a raising source doesn't take the chain down")
        lookup.SOURCES = (boom,)
        result = await lookup._run_lookup(app, "W1AW")
        check(result["status"] == lookup_cache.STATUS_ERROR
              and "kaboom" in result["error"],
              f"chain: a raising source presents as ERROR "
              f"(got {result['error']!r})")

        # ---- ...even a half-written one that never bound SOURCE ----
        # The error string is built inside the except block; naming the source
        # there must not be the thing that finally takes the chain down.
        nameless = type(sys)("stub_nameless")
        nameless.setup = lambda app: None
        nameless.lookup = _raise
        lookup.SOURCES = (nameless, _stub("rescue", lookup_cache.STATUS_OK))
        result = await lookup._run_lookup(app, "W1AW")
        check(result["status"] == lookup_cache.STATUS_OK,
              "chain: a raising source with no SOURCE doesn't take the chain down")
        lookup.SOURCES = (nameless,)
        result = await lookup._run_lookup(app, "W1AW")
        check(result["error"] == "stub_nameless: RuntimeError: kaboom",
              f"chain: a SOURCE-less source falls back to its module name "
              f"(got {result['error']!r})")

        # ---- an async source is awaited ----
        aio = type(sys)("stub_async")
        aio.SOURCE = "aio"
        aio.CACHED = False
        aio.setup = lambda app: None
        async def _async_lookup(app, callsign):
            return {"status": lookup_cache.STATUS_OK,
                    "payload": {"callsign": callsign, "source": "aio"},
                    "error": ""}
        aio.lookup = _async_lookup
        lookup.SOURCES = (aio,)
        result = await lookup._run_lookup(app, "W1AW")
        check(result["status"] == lookup_cache.STATUS_OK
              and result["payload"]["source"] == "aio",
              "chain: an async source's result is awaited, not returned raw")

        app["lookup_cache"].close()
    finally:
        lookup.SOURCES = original
        shutil.rmtree(scratch, ignore_errors=True)


def check_fcc_unit():
    """Drive the FCC adapter directly against a scratch fixture, without
    the server / HTTP layer. Locks in the row -> canonical mapping and
    the zone-derivation path."""
    import lookup_fcc
    scratch = Path(tempfile.mkdtemp(prefix="haml-fcc-unit-"))
    try:
        fcc_path = scratch / "lookup_data.sqlite"
        build_fixture(fcc_path)

        class _App(dict):
            pass
        app = _App()
        app["cfg"] = {"lookup_db_path": str(fcc_path)}
        lookup_fcc.setup(app)
        check(app.get("lookup_db") is not None,
              "fcc.setup() opens the DB on a valid file")
        check(app.get("lookup_db_path") == str(fcc_path),
              "fcc.setup() stashes the resolved path")

        # ---- W1AW: Individual, has previous_callsign, has coords ----
        result = lookup_fcc.lookup(app, "W1AW")
        check(result["status"] == lookup_cache.STATUS_OK,
              "W1AW -> STATUS_OK")
        rec = result["payload"]
        # Name composed from components, NOT entity_name "MONKS, WILLIAM S".
        check(rec["name"] == "JOSHUA D VILLWOCK",
              f"W1AW name built from components (got {rec['name']!r})")
        check(rec["callsign"] == "W1AW", "W1AW callsign")
        check(rec["license_type"] == "person", "W1AW license_type=person")
        check(rec["license_class"] == "extra", "W1AW license_class=extra")
        check(rec["previous_callsign"] == "KG7WKU", "W1AW previous_callsign")
        check(rec["previous_license_class"] == "general",
              "W1AW previous_license_class=general")
        check(rec["trustee_callsign"] is None, "W1AW no trustee")
        check(rec["address_line1"] == "14970 SALT CREEK RD",
              "W1AW address_line1 from street_address")
        # address_line2 must match the client's state regex AND extract OR.
        check(rec["address_line2"] == "DALLAS, OR 97338",
              f"W1AW address_line2 == 'DALLAS, OR 97338' "
              f"(got {rec['address_line2']!r})")
        m = STATE_IN_ADDRESS_RE.search(rec["address_line2"])
        check(m and m.group(1) == "OR",
              f"W1AW address_line2 parses OR via client regex "
              f"(got {m.group(1) if m else None!r})")
        check(m and VALID_STATES.intersection({m.group(1)}),
              "W1AW extracted state is in the client's accepted set")
        check(rec["state"] == "OR",
              f"W1AW state is the 2-letter code (got {rec['state']!r})")
        check(rec["county"] == "Polk",
              f"W1AW county from DB column (got {rec['county']!r})")
        check(rec["section"] == "OR",
              f"W1AW section from arrl_section column (got {rec['section']!r})")
        check(rec["country"] == "United States",
              f"W1AW country from DB column (got {rec['country']!r})")
        check(rec["continent"] == "NA",
              f"W1AW continent from DB column (got {rec['continent']!r})")
        check(rec["dxcc"] == 291,
              f"W1AW dxcc from DB column (got {rec['dxcc']!r})")
        check(rec["latitude"] == 44.979441, f"W1AW latitude (got {rec['latitude']!r})")
        check(rec["longitude"] == -123.337862,
              f"W1AW longitude (got {rec['longitude']!r})")
        check(rec["gridsquare"] == "CN84",
              f"W1AW gridsquare truncated to 4 chars (got {rec['gridsquare']!r})")
        check(rec["frn"] == "0024933376", "W1AW frn")
        check(rec["grant_date"] == "2024-03-19", "W1AW grant_date ISO")
        check(rec["expiry_date"] == "2034-03-19", "W1AW expiry_date ISO")
        check(rec["source"] == "fcc", "W1AW source=fcc")
        check(rec.get("fetched_at"),
              "W1AW fetched_at stamped")
        # Zones are NOT derived by the adapter any more — lookup_postprocess
        # fills them from the coordinates on the way out (see
        # check_postprocess_unit, which asserts CQ 3 / ITU 6 for these
        # coords). The adapter must hand them over null.
        check(rec["cq_zone"] is None,
              f"W1AW cq_zone left to the post-processor (got {rec['cq_zone']!r})")
        check(rec["itu_zone"] is None,
              f"W1AW itu_zone left to the post-processor (got {rec['itu_zone']!r})")
        # output keys must be exactly FIELDS
        check(set(rec.keys()) == set(lookup_record.FIELDS),
              "W1AW output keys == FIELDS exactly")

        # ---- W7CLB: Amateur Club, has trustee ----
        result = lookup_fcc.lookup(app, "W7CLB")
        check(result["status"] == lookup_cache.STATUS_OK,
              "W7CLB -> STATUS_OK")
        rec = result["payload"]
        check(rec["license_type"] == "club", "W7CLB license_type=club")
        check(rec["license_class"] is None, "W7CLB no license_class")
        check(rec["name"] == "TEST RADIO CLUB",
              "W7CLB name from entity_name (not components)")
        check(rec["trustee_callsign"] == "W7TRU", "W7CLB trustee_callsign")
        check(rec["trustee_name"] == "TEST TRUSTEE", "W7CLB trustee_name")

        # ---- N0BOX: PO-box-only licensee ----
        result = lookup_fcc.lookup(app, "N0BOX")
        rec = result["payload"]
        check(result["status"] == lookup_cache.STATUS_OK, "N0BOX -> STATUS_OK")
        check(rec["address_line1"] == "PO BOX 123",
              f"N0BOX address_line1 synthesized (got {rec['address_line1']!r})")
        check(rec["section"] == "OR",
              f"N0BOX lowercase section uppercased (got {rec['section']!r})")

        # ---- N0GEO: NULL coordinates ----
        result = lookup_fcc.lookup(app, "N0GEO")
        rec = result["payload"]
        check(result["status"] == lookup_cache.STATUS_OK, "N0GEO -> STATUS_OK")
        # (zones are null for every FCC record now — see W1AW above)
        check(rec["latitude"] is None,
              f"N0GEO latitude is None (got {rec['latitude']!r})")
        check(rec["longitude"] is None,
              f"N0GEO longitude is None (got {rec['longitude']!r})")
        check(rec["cq_zone"] is None,
              f"N0GEO cq_zone is None (got {rec['cq_zone']!r})")
        check(rec["itu_zone"] is None,
              f"N0GEO itu_zone is None (got {rec['itu_zone']!r})")
        check(rec["county"] is None,
              f"N0GEO empty county coerces to None (got {rec['county']!r})")
        check(rec["section"] is None,
              f"N0GEO empty section coerces to None (got {rec['section']!r})")
        check(rec["country"] is None,
              f"N0GEO empty country coerces to None (got {rec['country']!r})")
        check(rec["continent"] is None,
              f"N0GEO empty continent coerces to None (got {rec['continent']!r})")
        check(rec["dxcc"] is None,
              f"N0GEO NULL dxcc coerces to None (got {rec['dxcc']!r})")

        # ---- unknown callsign ----
        result = lookup_fcc.lookup(app, "ZZZZZZ")
        check(result["status"] == lookup_cache.STATUS_NOT_FOUND,
              "unknown call -> STATUS_NOT_FOUND")
        check(result["payload"] == {},
              "unknown call -> empty payload")
        check(result["error"] == "callsign not found",
              "unknown call -> standard 'callsign not found' error")

        # ---- missing-DB setup ----
        scratch2 = Path(tempfile.mkdtemp(prefix="haml-fcc-missing-"))
        try:
            class _App2(dict):
                pass
            app2 = _App2()
            app2["cfg"] = {"lookup_db_path": str(scratch2 / "absent.sqlite")}
            lookup_fcc.setup(app2)
            check(app2.get("lookup_db") is None,
                  "fcc.setup() with a missing file -> app['lookup_db'] is None")
            check(app2.get("lookup_db_path") == str(scratch2 / "absent.sqlite"),
                  "fcc.setup() still stashes the resolved path on missing file")
            result = lookup_fcc.lookup(app2, "W1AW")
            check(result["status"] == lookup_cache.STATUS_ERROR,
                  "missing-DB lookup -> STATUS_ERROR")
            check("unavailable" in result["error"].lower(),
                  f"missing-DB error mentions unavailability "
                  f"(got {result['error']!r})")
        finally:
            shutil.rmtree(scratch2, ignore_errors=True)

        app["lookup_db"].close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- ISED (Canada) fixture --------------------------------------------------
# The Canadian dataset is built to the FCC column layout (arrl_section
# included), so it reuses OPERATORS_SCHEMA. Rows exercise the
# three ways the CA adapter differs from FCC: a province state code, the
# multi-letter qualification set collapsing to one class word, and a club
# with no license dates / previous history (all NULL in this dataset).
CA_FIXTURE = [
    # VE7ADV: Individual, "AD" = Basic + Advanced -> "advanced" (D outranks A).
    # Province code BC must survive coercion (was US-only before this source).
    {
        "callsign": "VE7ADV",
        "applicant_type": "Individual",
        "first_name": "Bill", "middle_initial": None, "last_name": "McFadden",
        "name_suffix": None,
        "entity_name": "Bill McFadden",
        "operator_class": "AD", "previous_operator_class": None,
        "previous_callsign": None,
        "trustee_callsign": None, "trustee_name": None,
        "street_address": "188 MILLWOOD DRIVE", "po_box": None,
        "city": "VANCOUVER", "state": "BC", "zip_code": "V6B 1A1",
        "attention_line": None,
        "frn": None,
        "grant_date": None, "expired_date": None,
        "gridsquare": "CN89ng",
        "coordinates": "49.2827,-123.1207",
        "county": "Greater Vancouver",
        "arrl_section": "BC",
        "dxcc_entity": "Canada",
        "continent": "NA",
        "dxcc_id": 1,
    },
    # VA3HON: Individual, "E" only = "basic with honours".
    {
        "callsign": "VA3HON",
        "applicant_type": "Individual",
        "first_name": "Jane", "middle_initial": None, "last_name": "Doe",
        "name_suffix": None,
        "entity_name": "Jane Doe",
        "operator_class": "E", "previous_operator_class": None,
        "previous_callsign": None,
        "trustee_callsign": None, "trustee_name": None,
        "street_address": "1 King St", "po_box": None,
        "city": "TORONTO", "state": "ON", "zip_code": "M5H 1A1",
        "attention_line": None,
        "frn": None,
        "grant_date": None, "expired_date": None,
        "gridsquare": "FN03",
        "coordinates": "43.6532,-79.3832",
        "county": None,
        "arrl_section": "GTA",
        "dxcc_entity": "Canada",
        "continent": "NA",
        "dxcc_id": 1,
    },
    # VE1CWO: Individual holding ONLY CW endorsements ("BC" = 5+12 WPM) with
    # no A/D/E privilege -> class collapses to empty -> clean None.
    {
        "callsign": "VE1CWO",
        "applicant_type": "Individual",
        "first_name": "Cw", "middle_initial": None, "last_name": "Only",
        "name_suffix": None,
        "entity_name": "Cw Only",
        "operator_class": "BC", "previous_operator_class": None,
        "previous_callsign": None,
        "trustee_callsign": None, "trustee_name": None,
        "street_address": None, "po_box": None,
        "city": None, "state": "NS", "zip_code": None,
        "attention_line": None,
        "frn": None,
        "grant_date": None, "expired_date": None,
        "gridsquare": None,
        "coordinates": None,
        "county": None,
        "arrl_section": None,
        "dxcc_entity": "Canada",
        "continent": "NA",
        "dxcc_id": 1,
    },
    # VA1ADV: Amateur Club. Name from entity_name; trustee_name populated;
    # license_type=club so the client skips the name fill.
    {
        "callsign": "VA1ADV",
        "applicant_type": "Amateur Club",
        "first_name": None, "middle_initial": None, "last_name": None,
        "name_suffix": None,
        "entity_name": "Advocate Fire Department",
        "operator_class": "ACD", "previous_operator_class": None,
        "previous_callsign": None,
        "trustee_callsign": None, "trustee_name": "James Russel Hannon",
        "street_address": "PO BOX 126", "po_box": None,
        "city": "ADVOCATE HARBOUR", "state": "NS", "zip_code": "B0M 1A0",
        "attention_line": None,
        "frn": None,
        "grant_date": None, "expired_date": None,
        "gridsquare": "FN75oi",
        "coordinates": "45.333367,-64.777525",
        "county": "Cumberland",
        "arrl_section": "NS",
        "dxcc_entity": "Canada",
        "continent": "NA",
        "dxcc_id": 1,
    },
]


def check_ca_unit():
    """Drive the ISED (Canada) adapter directly against a scratch fixture.
    Locks in the row -> canonical mapping, the qualification-set -> class-word
    collapse, province-code survival, and the always-NULL columns."""
    import lookup_ca
    scratch = Path(tempfile.mkdtemp(prefix="haml-ca-unit-"))
    try:
        ca_path = scratch / "lookup_data.sqlite"
        build_fixture(ca_path)

        class _App(dict):
            pass
        app = _App()
        app["cfg"] = {"lookup_db_path": str(ca_path)}
        lookup_ca.setup(app)
        check(app.get("lookup_db") is not None,
              "ca.setup() opens the DB on a valid file")
        check(app.get("lookup_db_path") == str(ca_path),
              "ca.setup() stashes the resolved path")

        # ---- VE7ADV: Individual, "AD" -> advanced, BC province ----
        result = lookup_ca.lookup(app, "VE7ADV")
        check(result["status"] == lookup_cache.STATUS_OK, "VE7ADV -> STATUS_OK")
        rec = result["payload"]
        check(rec["source"] == "ised", "VE7ADV source=ised")
        check(rec.get("fetched_at"), "VE7ADV fetched_at stamped")
        check(rec["name"] == "Bill McFadden",
              f"VE7ADV name from first+last (got {rec['name']!r})")
        check(rec["license_type"] == "person", "VE7ADV license_type=person")
        check(rec["license_class"] == "advanced",
              f"VE7ADV 'AD' -> advanced (got {rec['license_class']!r})")
        check(rec["state"] == "BC",
              f"VE7ADV province code survives coercion (got {rec['state']!r})")
        check(rec["section"] == "BC",
              f"VE7ADV section from arrl_section column (got {rec['section']!r})")
        check(rec["address_line2"] == "VANCOUVER, BC V6B 1A1",
              f"VE7ADV address_line2 (got {rec['address_line2']!r})")
        check(rec["country"] == "Canada", "VE7ADV country=Canada")
        check(rec["continent"] == "NA", "VE7ADV continent=NA")
        check(rec["dxcc"] == 1, f"VE7ADV dxcc=1 (got {rec['dxcc']!r})")
        check(rec["gridsquare"] == "CN89",
              f"VE7ADV grid truncated to 4 (got {rec['gridsquare']!r})")
        # Columns ISED never publishes come back clean-None.
        check(rec["frn"] is None, "VE7ADV frn None (not published)")
        check(rec["grant_date"] is None, "VE7ADV grant_date None (not published)")
        check(rec["expiry_date"] is None, "VE7ADV expiry_date None (not published)")
        check(rec["previous_callsign"] is None,
              "VE7ADV previous_callsign None (not published)")
        # Zones left to the post-processor, same contract as FCC.
        check(rec["cq_zone"] is None and rec["itu_zone"] is None,
              "VE7ADV zones left to post-processor")
        check(set(rec.keys()) == set(lookup_record.FIELDS),
              "VE7ADV output keys == FIELDS exactly")

        # ---- VA3HON: "E" -> basic with honours ----
        rec = lookup_ca.lookup(app, "VA3HON")["payload"]
        check(rec["license_class"] == "basic with honours",
              f"VA3HON 'E' -> basic with honours (got {rec['license_class']!r})")
        check(rec["state"] == "ON", "VA3HON province ON")

        # ---- VE1CWO: CW-only endorsements -> no class word (clean None) ----
        res = lookup_ca.lookup(app, "VE1CWO")
        check(res["status"] == lookup_cache.STATUS_OK, "VE1CWO -> STATUS_OK")
        rec = res["payload"]
        check(rec["license_class"] is None,
              f"VE1CWO 'BC' collapses to None (got {rec['license_class']!r})")
        check(rec["section"] is None,
              f"VE1CWO NULL section is None (got {rec['section']!r})")

        # ---- VA1ADV: Amateur Club ----
        rec = lookup_ca.lookup(app, "VA1ADV")["payload"]
        check(rec["license_type"] == "club", "VA1ADV license_type=club")
        check(rec["name"] == "Advocate Fire Department",
              "VA1ADV name from entity_name")
        check(rec["trustee_name"] == "James Russel Hannon",
              "VA1ADV trustee_name populated")
        check(rec["address_line1"] == "PO BOX 126",
              f"VA1ADV address_line1 as-is (got {rec['address_line1']!r})")

        # ---- unknown callsign -> NOT_FOUND ----
        result = lookup_ca.lookup(app, "ZZZZZZ")
        check(result["status"] == lookup_cache.STATUS_NOT_FOUND,
              "unknown call -> STATUS_NOT_FOUND")
        check(result["error"] == "callsign not found",
              "unknown call -> standard 'callsign not found' error")

        # ---- missing-DB setup -> lookup errors ----
        scratch2 = Path(tempfile.mkdtemp(prefix="haml-ca-missing-"))
        try:
            class _App2(dict):
                pass
            app2 = _App2()
            app2["cfg"] = {"lookup_db_path": str(scratch2 / "absent.sqlite")}
            lookup_ca.setup(app2)
            check(app2.get("lookup_db") is None,
                  "ca.setup() with a missing file -> app['lookup_db'] is None")
            result = lookup_ca.lookup(app2, "VE7ADV")
            check(result["status"] == lookup_cache.STATUS_ERROR,
                  "missing-DB lookup -> STATUS_ERROR")
            check("unavailable" in result["error"].lower(),
                  f"missing-DB error mentions unavailability "
                  f"(got {result['error']!r})")
        finally:
            shutil.rmtree(scratch2, ignore_errors=True)

        app["lookup_db"].close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_callparser_unit():
    """Drive the CallParser adapter directly with the repo's committed
    Prefix.lst. Locks in the raw -> canonical mapping: zones as ints,
    dxcc int (leading-zero-safe), coords floats, all US-only fields None.

    The callparser module's `init()` is process-global and idempotent
    (a successful load short-circuits subsequent calls), so we exercise
    the missing-file setup FIRST — before any successful load — to keep
    the not-ready assertion faithful to real boot semantics.
    """
    import lookup_callparser

    real_path = SERVER_DIR / "datasets" / "Prefix.lst"
    check(real_path.exists(),
          f"real Prefix.lst exists at {real_path}")

    # ---- not-ready setup FIRST: lookup returns STATUS_NOT_FOUND, not ERROR.
    # The chain treats not-ready the same as a miss so the caller's prior
    # FCC status decides the response (FCC error + CP not-ready = 502;
    # FCC miss + CP not-ready = found:false). Must run before any successful load
    # because callparser.init() short-circuits on a process-global
    # _loaded flag — once a successful load has happened, a bad-path
    # setup() can't reproduce the not-ready state.
    class _AppNR(dict):
        pass
    app_nr = _AppNR()
    app_nr["cfg"] = {"prefix_lst_path": "C:/nonexistent/Prefix.lst"}
    lookup_callparser.setup(app_nr)
    check(app_nr.get("callparser_ready") is False,
          "callparser.setup() with a missing file -> not ready")
    result = lookup_callparser.lookup(app_nr, "G4ABC")
    check(result["status"] == lookup_cache.STATUS_NOT_FOUND,
          f"not-ready G4ABC -> STATUS_NOT_FOUND "
          f"(got {result['status']!r})")
    check(result["error"] == "",
          "not-ready G4ABC -> empty error string")

    # ---- now load the real fixture ----
    class _App(dict):
        pass
    app = _App()
    app["cfg"] = {"prefix_lst_path": str(real_path)}
    lookup_callparser.setup(app)
    check(app.get("callparser_ready") is True,
          "callparser.setup() loads the committed Prefix.lst")

    # ---- G4ABC (England) ----
    result = lookup_callparser.lookup(app, "G4ABC")
    check(result["status"] == lookup_cache.STATUS_OK,
          f"G4ABC -> STATUS_OK (got {result['status']!r})")
    rec = result["payload"]
    check(set(rec.keys()) == set(lookup_record.FIELDS),
          "G4ABC output keys == FIELDS exactly")
    check(rec["callsign"] == "G4ABC", "G4ABC callsign")
    check(rec["country"] == "England", f"G4ABC country=England (got {rec['country']!r})")
    check(rec["continent"] == "EU", f"G4ABC continent=EU (got {rec['continent']!r})")
    check(isinstance(rec["cq_zone"], int) and rec["cq_zone"] == 14,
          f"G4ABC cq_zone is int 14 (got {rec['cq_zone']!r})")
    check(isinstance(rec["itu_zone"], int) and rec["itu_zone"] == 27,
          f"G4ABC itu_zone is int 27 (got {rec['itu_zone']!r})")
    check(isinstance(rec["dxcc"], int) and rec["dxcc"] == 223,
          f"G4ABC dxcc is int 223 (got {rec['dxcc']!r})")
    check(isinstance(rec["latitude"], float) and rec["latitude"] > 0,
          f"G4ABC latitude is positive float (got {rec['latitude']!r})")
    check(isinstance(rec["longitude"], float) and rec["longitude"] < 0,
          f"G4ABC longitude is negative float (got {rec['longitude']!r})")
    # Sparseness: everything not in the prefix DB must be a clean None,
    # not a dirty "" — otherwise coerce() would have flagged it.
    check(rec["name"] is None,
          f"G4ABC name is None (got {rec['name']!r})")
    check(rec["state"] is None,
          f"G4ABC state is None (got {rec['state']!r})")
    check(rec["address_line1"] is None,
          f"G4ABC address_line1 is None (got {rec['address_line1']!r})")
    check(rec["license_type"] is None,
          f"G4ABC license_type is None (got {rec['license_type']!r})")
    check(rec["gridsquare"] is None,
          f"G4ABC gridsquare is None (got {rec['gridsquare']!r})")
    check(rec["frn"] is None,
          f"G4ABC frn is None (got {rec['frn']!r})")
    # Source/fetched_at stamped by the adapter.
    check(rec["source"] == "callparser",
          f"G4ABC source=callparser (got {rec['source']!r})")
    check(rec.get("fetched_at"),
          "G4ABC fetched_at stamped")

    # ---- EA8/W1AW: portable prefix resolves via CP ----
    result = lookup_callparser.lookup(app, "EA8/W1AW")
    check(result["status"] == lookup_cache.STATUS_OK,
          f"EA8/W1AW -> STATUS_OK (got {result['status']!r})")
    rec = result["payload"]
    check(rec["country"] == "Canary Is.",
          f"EA8/W1AW country=Canary Is. (got {rec['country']!r})")
    # Canary Is. ADIF is "029" in Prefix.lst — must coerce to int 29
    # through _coerce_zone(1, 999) (which uses float() then int()).
    check(isinstance(rec["dxcc"], int) and rec["dxcc"] == 29,
          f"EA8/W1AW dxcc is int 29 from '029' (got {rec['dxcc']!r})")

    # ---- garbage calls CP can't parse -> STATUS_NOT_FOUND ----
    for garbage in ("123ABC", "X", "ZZZZZZ"):
        result = lookup_callparser.lookup(app, garbage)
        check(result["status"] == lookup_cache.STATUS_NOT_FOUND,
              f"{garbage} -> STATUS_NOT_FOUND "
              f"(got {result['status']!r})")
        check(result["payload"] == {},
              f"{garbage} -> empty payload")
        check(result["error"] == "callsign not found",
              f"{garbage} -> standard 'callsign not found' error")


# --- end-to-end against the live server ------------------------------------
def _make_minimal_event_db(tmp):
    """Write a minimal event DB into tmp/events/ and a state.json pointing
    at it, so the server has an active event to bind to.
    """
    events_dir = tmp / "events"
    events_dir.mkdir(parents=True)
    event_db = events_dir / "test.db"
    conn = sqlite3.connect(event_db)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE contacts (
          uuid TEXT PRIMARY KEY,
          qso_at TEXT NOT NULL, created_at TEXT NOT NULL,
          last_edited TEXT NOT NULL, synced_at TEXT NOT NULL,
          remote_callsign TEXT NOT NULL, operator_callsign TEXT NOT NULL,
          operator_initials TEXT NOT NULL, client_uuid TEXT NOT NULL,
          band TEXT NOT NULL, mode TEXT NOT NULL,
          country TEXT NOT NULL DEFAULT '', itu_zone TEXT NOT NULL DEFAULT '',
          cq_zone TEXT NOT NULL DEFAULT '', continent TEXT NOT NULL DEFAULT '',
          gridsquare TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
          section TEXT NOT NULL DEFAULT '', frequency TEXT NOT NULL DEFAULT '',
          rst_sent TEXT NOT NULL DEFAULT '', rst_received TEXT NOT NULL DEFAULT '',
          name TEXT NOT NULL DEFAULT '',
          deleted INTEGER NOT NULL DEFAULT 0, fields TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE chat (uuid TEXT PRIMARY KEY, sent_at TEXT NOT NULL,
          operator_callsign TEXT NOT NULL, operator_initials TEXT NOT NULL,
          client_uuid TEXT NOT NULL, text TEXT NOT NULL);
    """)
    conn.execute("INSERT INTO meta VALUES ('event_uuid', 'test-uuid')")
    conn.execute("INSERT INTO meta VALUES ('event_name', 'smoke-lookup')")
    conn.execute("INSERT INTO meta VALUES ('station_callsign', 'TEST')")
    # Operating position: Portland, OR. W1AW's fixture coords are Dallas, OR
    # — 78 km away by the server's Haversine formula — so the e2e can
    # assert an exact `distance` in the lookup response.
    conn.execute(
        """INSERT INTO meta VALUES ('config',
           '{"location": {"latitude": 45.5152, "longitude": -122.6784}}')""")
    conn.commit()
    conn.close()
    (tmp / "state.json").write_text(
        json.dumps({"active": "events/test.db"}))


def _make_config(tmp, lookup_db_path, prefix_lst_path=None,
                 ultracheck_db_path=None):
    cfg = {
        "host": "127.0.0.1", "port": PORT,
        "data_dir": str(tmp), "admin_password": "test-pw",
        # One path for both offline licensee sources: the fixture carries
        # `fcc_operators` and `ca_operators` exactly as production does. The
        # CA table is a clean NOT_FOUND for the US/garbage calls these e2e
        # cases probe, preserving the miss/502 fall-through — never an
        # "unavailable" error that would turn an expected miss into a 502.
        "lookup_db_path": str(lookup_db_path),
    }
    if prefix_lst_path is not None:
        cfg["prefix_lst_path"] = str(prefix_lst_path)
    # Always set, even to a nonexistent path: the default resolves to the real
    # 91 MB datasets/ultracheck.sqlite, which the suite must never read.
    cfg["ultracheck_db_path"] = str(
        ultracheck_db_path if ultracheck_db_path is not None
        else tmp / "no_ultracheck.sqlite")
    return tmp / "config.json", json.dumps(cfg)


async def run_e2e(lookup_db_path, prefix_lst_path=None,
                  missing_db=False, missing_prefix_lst=False,
                  ultracheck_db_path=None):
    preclean()
    tmp = Path(tempfile.mkdtemp(prefix="haml-lookup-"))
    try:
        if missing_db:
            db_path = tmp / "does_not_exist.sqlite"
        else:
            db_path = lookup_db_path
        if prefix_lst_path is None and not missing_prefix_lst:
            # Default to the committed fixture so the chain has DX coverage.
            prefix_lst_path = SERVER_DIR / "datasets" / "Prefix.lst"
        if missing_prefix_lst:
            cp_path = tmp / "does_not_exist_Prefix.lst"
        else:
            cp_path = prefix_lst_path
        config_path, body = _make_config(tmp, db_path,
                                         prefix_lst_path=cp_path,
                                         ultracheck_db_path=ultracheck_db_path)
        config_path.write_text(body)
        _make_minimal_event_db(tmp)

        proc = start_server(config_path)
        try:
            async with aiohttp.ClientSession() as session:
                if missing_db:
                    if missing_prefix_lst:
                        # Both hops absent: today's 502 path is preserved
                        # exactly (FCC error + CP not-ready => original
                        # FCC result returned).
                        print("missing-DB + missing-CP -> 502:")
                        status, b = await post_lookup(session, "W1AW")
                        check(status == 502,
                              f"missing-DB+missing-CP W1AW -> 502 "
                              f"(got {status})")
                        check("unavailable" in b.get("error", "").lower(),
                              f"502 mentions unavailability "
                              f"(got {b.get('error')!r})")
                        return
                    # FCC dataset absent, CP loaded: CP hop handles every
                    # prefix-DB-resolvable callsign. W1AW IS resolvable
                    # by CP (prefix 'W' -> United States), so it returns
                    # 200 with source="callparser". A truly garbage call
                    # that neither hop resolves preserves the 502
                    # (FCC error returned verbatim).
                    print("missing-DB config -> CP fallback:")
                    status, b = await post_lookup(session, "W1AW")
                    check(status == 200,
                          f"missing-DB W1AW resolves via CP -> 200 "
                          f"(got {status})")
                    check(b.get("source") == "callparser",
                          f"missing-DB W1AW source=callparser "
                          f"(got {b.get('source')!r})")
                    check(b.get("country") == "United States of America",
                          f"missing-DB W1AW country (got {b.get('country')!r})")
                    # This run also has no ultracheck DB (no path is ever
                    # allowed to fall back to the real 91 MB dataset), which
                    # exercises the degrade path: the key is still there with
                    # every source present and empty, and `available` says why.
                    # A missing partial-search dataset must not cost a lookup.
                    uc = b.get("ultracheck") or {}
                    check(uc.get("available") is False,
                          f"no ultracheck DB -> available false "
                          f"(got {uc.get('available')!r})")
                    check(set(uc.get("sources") or {}) == set(_UC_SOURCES),
                          "no ultracheck DB -> every source key still present")
                    check(all(not s["matches"]
                              for s in uc["sources"].values()),
                          "no ultracheck DB -> every match list empty")
                    # Resolvable DX call.
                    status, b = await post_lookup(session, "G4ABC")
                    check(status == 200,
                          f"missing-DB G4ABC (resolvable via CP) -> 200 "
                          f"(got {status})")
                    check(b.get("source") == "callparser",
                          f"missing-DB G4ABC source=callparser "
                          f"(got {b.get('source')!r})")
                    check(b.get("country") == "England",
                          f"missing-DB G4ABC country=England "
                          f"(got {b.get('country')!r})")
                    # An unresolvable garbage call keeps the 502 visible:
                    # FCC error returned verbatim because CP also missed.
                    status, b = await post_lookup(session, "123ABC")
                    check(status == 502,
                          f"missing-DB 123ABC (CP-miss) -> 502 "
                          f"(got {status})")
                    check("unavailable" in b.get("error", "").lower(),
                          f"502 mentions unavailability "
                          f"(got {b.get('error')!r})")
                    return

                # ---- cold Individual (W1AW) ----
                print("cold Individual (W1AW):")
                t0 = time.monotonic()
                status, body = await post_lookup(session, "W1AW")
                cold_ms = (time.monotonic() - t0) * 1000
                check(status == 200, f"cold W1AW -> 200 (got {status})")
                check(body.get("found") is True,
                      f"W1AW found=true (got {body.get('found')!r})")
                check(body.get("callsign") == "W1AW",
                      f"W1AW callsign (got {body.get('callsign')!r})")
                check(body.get("name") == "JOSHUA D VILLWOCK",
                      f"W1AW name built from components "
                      f"(got {body.get('name')!r})")
                check(body.get("license_type") == "person",
                      "W1AW license_type=person")
                check(body.get("license_class") == "extra",
                      "W1AW license_class=extra")
                check(body.get("source") == "fcc",
                      "W1AW source=fcc")
                check("fetched_at" in body,
                      "W1AW payload has fetched_at")
                check("DALLAS, OR 97338" in (body.get("address_line2") or ""),
                      f"W1AW address_line2 shaped for client parse "
                      f"(got {body.get('address_line2')!r})")
                m = STATE_IN_ADDRESS_RE.search(body.get("address_line2", ""))
                check(m and m.group(1) == "OR",
                      f"W1AW client regex extracts OR (got "
                      f"{m.group(1) if m else None!r})")
                check(body.get("state") == "OR",
                      f"W1AW state field is 'OR' (got {body.get('state')!r})")
                check(body.get("county") == "Polk",
                      f"W1AW county is 'Polk' (got {body.get('county')!r})")
                check(body.get("section") == "OR",
                      f"W1AW section is 'OR' (got {body.get('section')!r})")
                check(body.get("country") == "United States",
                      f"W1AW country is 'United States' "
                      f"(got {body.get('country')!r})")
                check(body.get("continent") == "NA",
                      f"W1AW continent is 'NA' "
                      f"(got {body.get('continent')!r})")
                check(body.get("dxcc") == 291,
                      f"W1AW dxcc is 291 (got {body.get('dxcc')!r})")
                check(isinstance(body.get("latitude"), float)
                      and body["latitude"] == 44.979441,
                      "W1AW latitude is float 44.979441")
                check(isinstance(body.get("longitude"), float)
                      and body["longitude"] == -123.337862,
                      "W1AW longitude is float -123.337862")
                check(body.get("cq_zone") == 3,
                      f"W1AW cq_zone == 3 (Dallas, OR; got "
                      f"{body.get('cq_zone')!r})")
                check(body.get("itu_zone") == 6,
                      f"W1AW itu_zone == 6 (Dallas, OR; got "
                      f"{body.get('itu_zone')!r})")
                check(re.match(r"^\d{4}-\d{2}-\d{2}$",
                               body.get("grant_date", "")),
                      f"W1AW grant_date is YYYY-MM-DD "
                      f"(got {body.get('grant_date')!r})")
                # Event location is Portland, OR; W1AW is Dallas, OR.
                check(body.get("distance") == 78,
                      f"W1AW distance == 78 km from event location "
                      f"(got {body.get('distance')!r})")
                print(f"  ({cold_ms:.0f}ms cold)")

                # ---- warm re-hit (FCC always recomputes; check it stays fast) ----
                t0 = time.monotonic()
                status2, body2 = await post_lookup(session, "W1AW")
                warm_ms = (time.monotonic() - t0) * 1000
                check(status2 == 200, f"warm W1AW -> 200 (got {status2})")
                check(body2.get("callsign") == "W1AW",
                      "warm W1AW callsign")
                check(warm_ms < cold_ms / 2,
                      f"warm W1AW ({warm_ms:.0f}ms) faster than cold "
                      f"({cold_ms:.0f}ms)")
                print(f"  ({warm_ms:.0f}ms warm)")

                # ---- suffix normalization (W1AW/P) ----
                print("suffix normalization (W1AW/P):")
                status, body = await post_lookup(session, "W1AW/P")
                check(status == 200,
                      f"W1AW/P -> 200 (got {status})")
                check(body.get("callsign") == "W1AW",
                      "suffix stripped before FCC lookup")

                # ---- cold Amateur Club (W7CLB) ----
                print("cold Amateur Club (W7CLB):")
                status, body = await post_lookup(session, "W7CLB")
                check(status == 200, f"W7CLB -> 200 (got {status})")
                check(body.get("license_type") == "club",
                      "W7CLB license_type=club")
                check(body.get("license_class") is None,
                      "W7CLB no license_class")
                check(body.get("name") == "TEST RADIO CLUB",
                      "W7CLB name from entity_name")
                check(body.get("trustee_callsign") == "W7TRU",
                      "W7CLB trustee_callsign")
                check(body.get("trustee_name") == "TEST TRUSTEE",
                      "W7CLB trustee_name")

                # ---- PO-box-only licensee (N0BOX) ----
                print("PO-box-only (N0BOX):")
                status, body = await post_lookup(session, "N0BOX")
                check(status == 200, f"N0BOX -> 200 (got {status})")
                check(body.get("address_line1") == "PO BOX 123",
                      f"N0BOX address_line1 synthesized (got "
                      f"{body.get('address_line1')!r})")

                # ---- NULL coordinates (N0GEO) ----
                print("NULL coordinates (N0GEO):")
                status, body = await post_lookup(session, "N0GEO")
                check(status == 200, f"N0GEO -> 200 (got {status})")
                check(body.get("latitude") is None,
                      f"N0GEO latitude is None (got "
                      f"{body.get('latitude')!r})")
                check(body.get("longitude") is None,
                      f"N0GEO longitude is None (got "
                      f"{body.get('longitude')!r})")
                check(body.get("cq_zone") is None,
                      f"N0GEO cq_zone is None (got "
                      f"{body.get('cq_zone')!r})")
                check(body.get("distance") is None,
                      f"N0GEO distance is None without coords (got "
                      f"{body.get('distance')!r})")
                check(body.get("itu_zone") is None,
                      f"N0GEO itu_zone is None (got "
                      f"{body.get('itu_zone')!r})")
                check(body.get("country") is None,
                      f"N0GEO country is None (got "
                      f"{body.get('country')!r})")
                check(body.get("continent") is None,
                      f"N0GEO continent is None (got "
                      f"{body.get('continent')!r})")
                check(body.get("dxcc") is None,
                      f"N0GEO dxcc is None (got "
                      f"{body.get('dxcc')!r})")

                # ---- live ISED hit through the chain (Canadian call) ----
                # The FCC fixture holds only US calls, so a VE/VA callsign
                # misses FCC and must fall through to the ISED source. This is
                # the one e2e case that lands on lookup_ca via the live chain —
                # unit tests drive that adapter directly and can't catch a
                # SOURCES-wiring or config-plumbing bug. The row comes out of
                # the fixture's `ca_operators` table, the same file the FCC
                # rows live in — which is also the point being proven here:
                # two sources, two tables, one shared connection.
                print("live ISED hit (Canadian call):")
                ca_call = CA_FIXTURE[0]["callsign"]
                status, body = await post_lookup(session, ca_call)
                check(status == 200,
                      f"Canadian call {ca_call} -> 200 (got {status})")
                check(body.get("source") == "ised",
                      f"{ca_call} resolves via the ISED source through the chain "
                      f"(got {body.get('source')!r})")
                check(body.get("callsign") == ca_call,
                      f"{ca_call} callsign echoed (got {body.get('callsign')!r})")
                check(body.get("country") == "Canada",
                      f"{ca_call} country=Canada (got {body.get('country')!r})")

                # ---- ultracheck rides along on every response ----
                # Same object on a hit and on a miss, keyed on what the client
                # ASKED for. The fixture's W1AW row is the worst by every
                # metric, so it leading every list is exact-first working.
                print("ultracheck (hit): W1AW")
                status, body = await post_lookup(session, "W1AW")
                check(status == 200, f"W1AW -> 200 (got {status})")
                uc = body.get("ultracheck") or {}
                check(uc.get("available") is True,
                      f"ultracheck available (got {uc.get('available')!r})")
                check(uc.get("query") == "W1AW",
                      f"ultracheck echoes the query (got {uc.get('query')!r})")
                srcs = uc.get("sources") or {}
                check(set(srcs) == set(_UC_SOURCES),
                      f"ultracheck has every source key (got {sorted(srcs)})")
                for name in _UC_SOURCES:
                    calls = [m["callsign"] for m in srcs[name]["matches"]]
                    check(calls and calls[0] == "W1AW",
                          f"ultracheck {name}: exact match leads (got {calls})")
                    check("K9XYZ" not in calls,
                          f"ultracheck {name}: non-matching call absent")
                # Per-source ordering, once exact-first has had its say. The
                # expected list is the FULL fixture order clipped to whatever
                # that source's limit is currently set to, so tuning
                # SOURCE_LIMITS doesn't invalidate these — the limits are meant
                # to be tuned, and a test that hardcodes them just breaks.
                _uc_expect(srcs, "fd", ["W1AW", "W1AWX", "KW1AW", "W1AWZZ"],
                           "newest year first after the exact match")
                _uc_expect(srcs, "wfd", ["W1AW", "W1AWX", "KW1AW", "W1AWZZ"],
                           "newest year first after the exact match")
                _uc_expect(srcs, "pota_hunter",
                           ["W1AW", "W1AWX", "KW1AW", "W1AWZZ"],
                           "most QSOs first")
                _uc_expect(srcs, "pota_activator",
                           ["W1AW", "W1AWX", "KW1AW", "W1AWZZ"],
                           "most activations first")
                _uc_expect(srcs, "lotw",
                           ["W1AW", "W1AWX", "KW1AW", "W1AWZZ", "W1AWB",
                            "W1AWA"],
                           "most recent upload first")
                # W1AWQ is a Club Log member with no last QSO: last, not gone.
                _uc_expect(srcs, "clublog",
                           ["W1AW", "W1AWX", "KW1AW", "W1AWZZ", "W1AWQ"],
                           "most recent QSO first, null date last")
                # SCP is membership-only: shortest call, then best-attested.
                _uc_expect(srcs, "scp", ["W1AW", "KW1AW", "W1AWX", "W1AWZZ"],
                           "shortest then best-attested")
                # The metric rides along with the match and is what was sorted on.
                check([m["value"] for m in srcs["pota_hunter"]["matches"]][:4]
                      == [1, 999, 500, 250],
                      f"ultracheck pota_hunter: value is the QSO count "
                      f"(got {[m['value'] for m in srcs['pota_hunter']['matches']]})")
                check(all(m["value"] is None for m in srcs["scp"]["matches"]),
                      "ultracheck scp: membership-only, so every value is null")
                check(srcs["clublog"]["matches"][-1]["value"] is None
                      if len(srcs["clublog"]["matches"]) == 5 else True,
                      "ultracheck clublog: the last match is the null-date one")

                # A half-typed callsign — the case the feature exists for. No
                # row equals "1AW", so nothing is forced to the top and each
                # source's own ordering is on show unmodified.
                # (The chain itself may well resolve this: CallParser reads
                # "1A" as a real prefix. Whether it does is beside the point —
                # ultracheck runs either way, which is what's asserted here.)
                print("ultracheck (partial term): 1AW")
                status, body = await post_lookup(session, "1AW")
                check(status == 200, f"partial 1AW -> 200 (got {status})")
                uc = body.get("ultracheck") or {}
                check(uc.get("query") == "1AW", "ultracheck ran on the partial")
                # W1AW is no longer forced to the front, so fd is pure year
                # order and its 2010 row falls to last.
                _uc_expect(uc["sources"], "fd",
                           ["W1AWX", "KW1AW", "W1AWZZ", "W1AW"],
                           "no exact match, so pure year order")
                # scp: shortest first, then source_count desc within a length
                # tie (KW1AW knows 7 sources, W1AWX 6).
                _uc_expect(uc["sources"], "scp",
                           ["W1AW", "KW1AW", "W1AWX", "W1AWZZ"],
                           "by length then source_count, no exact match")

                # ---- cold unknown call: a miss is a 200, not a 404 ----
                # The full record shape with found:false, so the client reads
                # one field instead of branching on a status code.
                print("cold unknown call:")
                status, body = await post_lookup(session, "ZZZZZZ")
                check(status == 200, f"unknown ZZZZZZ -> 200 (got {status})")
                check(body.get("found") is False,
                      f"ZZZZZZ found=false (got {body.get('found')!r})")
                check("error" not in body,
                      "a miss body carries no error field")
                check(body.get("callsign") == "ZZZZZZ",
                      f"a miss still echoes the callsign "
                      f"(got {body.get('callsign')!r})")
                _check_miss_shape(body, "ZZZZZZ")
                # A miss still carries a searched ultracheck object — the case
                # the whole integration exists for. Nothing in the fixture
                # contains "ZZZZZZ", so it's available with empty lists.
                uc = body["ultracheck"]
                check(uc["available"] is True and uc["query"] == "ZZZZZZ",
                      "a lookup miss still ran an ultracheck search")
                check(all(not s["matches"] for s in uc["sources"].values()),
                      "ZZZZZZ matches no partial either")

                # ---- bad input: empty ----
                print("bad input:")
                status, body = await post_lookup(session, "")
                check(status == 400, f"empty -> 400 (got {status})")

                # ---- bad input: non-JSON ----
                status, _ = await post_raw(session, b"not json")
                check(status == 400, f"non-JSON body -> 400 (got {status})")

                # ---- bad input: missing callsign ----
                async with session.post(BASE + "/api/lookup",
                                        json={"foo": "bar"},
                                        timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    status = resp.status
                check(status == 400, f"missing callsign -> 400 (got {status})")

                # ---- coalescing: two concurrent POSTs share one drive ----
                print("coalescing:")
                fresh = "K1MI"  # a callsign we haven't looked up yet
                t0 = time.monotonic()
                (s1, b1), (s2, b2) = await asyncio.gather(
                    post_lookup(session, fresh),
                    post_lookup(session, fresh),
                )
                coalesce_ms = (time.monotonic() - t0) * 1000
                check(s1 == 200 and s2 == 200,
                      f"both coalesced lookups 200 (got {s1}, {s2})")
                check(b1.get("callsign") == fresh
                      and b2.get("callsign") == fresh,
                      "coalesced clients both get the right call")
                print(f"  (coalesce round-trip {coalesce_ms:.0f}ms)")

                # ---- K1MI (no previous) ----
                print("Individual no previous (K1MI):")
                status, body = await post_lookup(session, "K1MI")
                check(status == 200, f"K1MI -> 200 (got {status})")
                check(body.get("callsign") == "K1MI", "K1MI callsign")
                check(body.get("license_type") == "person",
                      "K1MI license_type=person")
                check(body.get("license_class") == "general",
                      "K1MI license_class=general")
                check(body.get("previous_callsign") is None,
                      f"K1MI previous_callsign is None (got "
                      f"{body.get('previous_callsign')!r})")

                # ---- DX call -> CallParser hop ----
                # G4ABC is not in the FCC fixture; the FCC hop misses
                # and CallParser fills in DXCC-level fields. The chain
                # must NOT regress on the FCC fixture: W1AW's US fields
                # are still served by the FCC hop (FCC wins on OK).
                print("DX call via CallParser (G4ABC):")
                status, body = await post_lookup(session, "G4ABC")
                check(status == 200, f"G4ABC -> 200 (got {status})")
                check(body.get("source") == "callparser",
                      f"G4ABC source=callparser "
                      f"(got {body.get('source')!r})")
                check(body.get("callsign") == "G4ABC", "G4ABC callsign")
                check(body.get("country") == "England",
                      f"G4ABC country=England "
                      f"(got {body.get('country')!r})")
                check(body.get("continent") == "EU",
                      f"G4ABC continent=EU "
                      f"(got {body.get('continent')!r})")
                check(body.get("cq_zone") == 14,
                      f"G4ABC cq_zone=14 (England; got "
                      f"{body.get('cq_zone')!r})")
                check(body.get("itu_zone") == 27,
                      f"G4ABC itu_zone=27 (England; got "
                      f"{body.get('itu_zone')!r})")
                check(body.get("dxcc") == 223,
                      f"G4ABC dxcc=223 (England ADIF; got "
                      f"{body.get('dxcc')!r})")
                check(isinstance(body.get("latitude"), float),
                      f"G4ABC latitude is float (got "
                      f"{type(body.get('latitude')).__name__})")
                check(isinstance(body.get("longitude"), float),
                      f"G4ABC longitude is float (got "
                      f"{type(body.get('longitude')).__name__})")
                # Sparseness: CP fills DXCC-level fields only. US-only
                # fields the entry form uses for fills must be null so
                # the client null-checks them out cleanly.
                check(body.get("name") is None,
                      f"G4ABC name is None (got {body.get('name')!r})")
                check(body.get("address_line1") is None,
                      f"G4ABC address_line1 is None (got "
                      f"{body.get('address_line1')!r})")
                check(body.get("address_line2") is None,
                      f"G4ABC address_line2 is None (got "
                      f"{body.get('address_line2')!r})")
                check(body.get("state") is None,
                      f"G4ABC state is None (got {body.get('state')!r})")
                check(body.get("license_type") is None,
                      f"G4ABC license_type is None (got "
                      f"{body.get('license_type')!r})")
                check(body.get("license_class") is None,
                      f"G4ABC license_class is None (got "
                      f"{body.get('license_class')!r})")
                # distance stamped by lookup_postprocess from entity-center coords.
                check(isinstance(body.get("distance"), int)
                      and body["distance"] > 0,
                      f"G4ABC distance is positive int "
                      f"(got {body.get('distance')!r})")
                # Source/fetched_at stamped by the adapter (cache layer
                # is bypassed for CP results).
                check("fetched_at" in body,
                      "G4ABC payload has fetched_at")

                # ---- FCC still wins on its own fixture (no regression) ----
                print("FCC fixture call (W1AW) - still source=fcc:")
                status, body = await post_lookup(session, "W1AW")
                check(status == 200, f"W1AW -> 200 (got {status})")
                check(body.get("source") == "fcc",
                      f"W1AW source=fcc even with CP loaded "
                      f"(got {body.get('source')!r})")
                check(body.get("name") == "JOSHUA D VILLWOCK",
                      f"W1AW name still from FCC components (got "
                      f"{body.get('name')!r})")

                # ---- CallParser rejects a callsign that has no prefix
                # match (digit-leading, too short, etc). FCC hop also
                # misses (not in fixture) -> CP miss -> found:false. The
                # chain returns the ORIGINAL FCC result on a miss so today
                # behavior is preserved.
                print("CallParser miss -> found:false:")
                status, body = await post_lookup(session, "123ABC")
                check(status == 200, f"123ABC -> 200 (got {status})")
                check(body.get("found") is False,
                      f"123ABC found=false (got {body.get('found')!r})")
                _check_miss_shape(body, "123ABC")

                # ---- Portable suffix resolves via CallParser ----
                # EA8/W1AW: prefix DB parses "EA8" as the prefix
                # (Canary Is.) and ignores the W1AW trailing call
                # (CallParser's _compare_ending rules). FCC doesn't
                # see this row in the fixture -> CP fills.
                print("Portable suffix via CallParser (EA8/W1AW):")
                status, body = await post_lookup(session, "EA8/W1AW")
                check(status == 200, f"EA8/W1AW -> 200 (got {status})")
                check(body.get("source") == "callparser",
                      f"EA8/W1AW source=callparser "
                      f"(got {body.get('source')!r})")
                check(body.get("country") == "Canary Is.",
                      f"EA8/W1AW country=Canary Is. "
                      f"(got {body.get('country')!r})")
                check(body.get("continent") == "AF",
                      f"EA8/W1AW continent=AF "
                      f"(got {body.get('continent')!r})")
                check(body.get("cq_zone") == 33,
                      f"EA8/W1AW cq_zone=33 (Canary Is.; got "
                      f"{body.get('cq_zone')!r})")
                check(body.get("itu_zone") == 36,
                      f"EA8/W1AW itu_zone=36 (Canary Is.; got "
                      f"{body.get('itu_zone')!r})")
                check(body.get("dxcc") == 29,
                      f"EA8/W1AW dxcc=29 (Canary Is. ADIF; got "
                      f"{body.get('dxcc')!r})")
        finally:
            stop_server(proc)
    finally:
        cleanup(tmp)


async def main():
    preclean()
    # Offline unit checks first — catch drift in TTL constants, coerce(),
    # and the fcc adapter's row -> canonical mapping without needing the
    # server.
    print("unit: TTL policy:")
    check_ttl_policy()
    print("unit: coerce() contract:")
    check_coerce()
    print("unit: post-processing (zones + distance):")
    check_postprocess_unit()
    print("unit: location derivations:")
    check_location_calc_unit()
    print("unit: operator overrides:")
    check_override_unit()
    print("unit: source chain shape:")
    check_chain_unit()
    print("unit: chain fall-through rules:")
    await check_chain_fallthrough_unit()
    print("unit: fcc adapter:")
    check_fcc_unit()
    print("unit: ised (canada) adapter:")
    check_ca_unit()
    print("unit: callparser adapter:")
    check_callparser_unit()

    print("\nend-to-end against the live server:")
    # Prefix must NOT start with "haml-lookup-": preclean() wipes those
    # between e2e runs and would delete the fixture out from under us.
    fixture_path = (Path(tempfile.mkdtemp(prefix="haml-fixture-"))
                    / "lookup_data.sqlite")
    uc_fixture_path = fixture_path.parent / "ultracheck.sqlite"
    try:
        build_fixture(fixture_path)
        build_ultracheck_fixture(uc_fixture_path)
        # Needs its fixture on disk, so it runs here rather than up with the
        # other offline unit checks; still no server involved.
        print("unit: ultracheck:")
        check_ultracheck_unit(uc_fixture_path)
        # Real Prefix.lst + lookup fixture: the new e2e CP cases run here.
        await run_e2e(fixture_path, missing_db=False,
                      ultracheck_db_path=uc_fixture_path)
        # Missing-DB, real Prefix.lst: CP must rescue every resolvable call.
        await run_e2e(fixture_path, missing_db=True)
        # Missing-DB + missing-CP: behavior matches today's exactly
        # (FCC error returned verbatim when CP also can't help).
        await run_e2e(fixture_path, missing_db=True, missing_prefix_lst=True)
    finally:
        cleanup(fixture_path.parent)

    print(f"\n{checks} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
