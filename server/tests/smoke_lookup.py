"""End-to-end smoke test for the callsign-lookup feature (offline, FCC-backed).

Stdlib-only. Spawns the real server on a scratch port with a scratch data
dir + a scratch FCC ULS fixture sqlite, then walks POST /api/lookup:

  - cold Individual: 200 with composed name "FIRST M LAST", license_type
    "person", address_line2 matching the client's state regex, derived
    zones, ISO dates, source "fcc"
  - warm re-hit: 200, same record
  - suffix normalization (W1AW/P): 200, same record
  - cold Amateur Club: 200, license_type "club", entity_name
  - PO-box-only licensee: address_line1 == "PO BOX 123"
  - NULL coordinates: 200, latitude/longitude None, zones None
  - cold unknown call: 404
  - previous_callsign value (not in the table): 404
  - bad input (empty): 400
  - bad input (non-JSON): 400
  - missing-DB config: 502
  - coalescing: two concurrent POSTs for the same cold callsign only
    resolve once
  - unit checks: TTL policy, coerce() contract (incl. ISO date acceptance),
    fcc adapter row -> canonical mapping

No internet access required. The fixture sqlite is built in scratch,
not the real 192MB dataset.

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
import lookup_cache    # noqa: E402
import lookup_record   # noqa: E402

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

checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ok: {label}")


# --- fixture ---------------------------------------------------------------
# Mirror of the production `operators` schema (see server/datasets/README.md).
# Real DB has 826k rows; the test only needs a handful to exercise the
# adapter's mapping + zones + status paths.
FCC_SCHEMA = """
CREATE TABLE operators (
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
  country               TEXT,
  continent             TEXT,
  dxcc                  INTEGER
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
    # in the table — proves a 404 for a "previous" value). entity_name is
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
        "country": "United States",
        "continent": "NA",
        "dxcc": 291,
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
        "country": "United States",
        "continent": "NA",
        "dxcc": 291,
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
        "country": "United States",
        "continent": "NA",
        "dxcc": 291,
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
        "country": "United States",
        "continent": "NA",
        "dxcc": 291,
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
        "country": "",
        "continent": "",
        "dxcc": None,
    },
]


def build_fixture(path):
    """Write the fixture sqlite at `path`. Returns the path."""
    conn = sqlite3.connect(path)
    conn.executescript(FCC_SCHEMA)
    cols = list(FCC_FIXTURE[0].keys())
    placeholders = ",".join("?" for _ in cols)
    for row in FCC_FIXTURE:
        conn.execute(
            f"INSERT INTO operators ({','.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )
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


def check_distance_unit():
    """Verify api_rest._with_distance stamps a request-time distance on the
    record: whole miles when both the event location and the record coords
    exist, null otherwise, and never mutates the input record."""
    import api_rest
    record = {"callsign": "W1AW", "latitude": 44.979441, "longitude": -123.337862}
    loc_app = {"event": {"config": {
        "location": {"latitude": 45.5152, "longitude": -122.6784}}}}

    out = api_rest._with_distance(loc_app, record)
    check(out["distance"] == 78,
          f"Portland -> Dallas OR == 78 km floored (got {out['distance']!r})")
    check("distance" not in record,
          "_with_distance leaves the input record unmodified")

    out = api_rest._with_distance(loc_app, {"latitude": None, "longitude": None})
    check(out["distance"] is None, "no record coords -> distance is None")

    out = api_rest._with_distance({"event": {"config": {"location": None}}}, record)
    check(out["distance"] is None, "no event location -> distance is None")

    out = api_rest._with_distance({}, record)
    check(out["distance"] is None, "no active event -> distance is None")


def check_fcc_unit():
    """Drive the FCC adapter directly against a scratch fixture, without
    the server / HTTP layer. Locks in the row -> canonical mapping and
    the zone-derivation path."""
    import fcc
    scratch = Path(tempfile.mkdtemp(prefix="haml-fcc-unit-"))
    try:
        fcc_path = scratch / "fcc.sqlite"
        build_fixture(fcc_path)

        class _App(dict):
            pass
        app = _App()
        app["cfg"] = {"fcc_db_path": str(fcc_path)}
        fcc.setup(app)
        check(app.get("fcc_db") is not None,
              "fcc.setup() opens the DB on a valid file")
        check(app.get("fcc_db_path") == str(fcc_path),
              "fcc.setup() stashes the resolved path")

        # ---- W1AW: Individual, has previous_callsign, has coords ----
        result = fcc.lookup(app, "W1AW")
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
        # Dallas, OR is CQ 3, ITU 6.
        check(rec["cq_zone"] == 3,
              f"W1AW cq_zone == 3 (Dallas, OR; got {rec['cq_zone']!r})")
        check(rec["itu_zone"] == 6,
              f"W1AW itu_zone == 6 (Dallas, OR; got {rec['itu_zone']!r})")
        # output keys must be exactly FIELDS
        check(set(rec.keys()) == set(lookup_record.FIELDS),
              "W1AW output keys == FIELDS exactly")

        # ---- W7CLB: Amateur Club, has trustee ----
        result = fcc.lookup(app, "W7CLB")
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
        result = fcc.lookup(app, "N0BOX")
        rec = result["payload"]
        check(result["status"] == lookup_cache.STATUS_OK, "N0BOX -> STATUS_OK")
        check(rec["address_line1"] == "PO BOX 123",
              f"N0BOX address_line1 synthesized (got {rec['address_line1']!r})")

        # ---- N0GEO: NULL coordinates ----
        result = fcc.lookup(app, "N0GEO")
        rec = result["payload"]
        check(result["status"] == lookup_cache.STATUS_OK, "N0GEO -> STATUS_OK")
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
        check(rec["country"] is None,
              f"N0GEO empty country coerces to None (got {rec['country']!r})")
        check(rec["continent"] is None,
              f"N0GEO empty continent coerces to None (got {rec['continent']!r})")
        check(rec["dxcc"] is None,
              f"N0GEO NULL dxcc coerces to None (got {rec['dxcc']!r})")

        # ---- unknown callsign ----
        result = fcc.lookup(app, "ZZZZZZ")
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
            app2["cfg"] = {"fcc_db_path": str(scratch2 / "absent.sqlite")}
            fcc.setup(app2)
            check(app2.get("fcc_db") is None,
                  "fcc.setup() with a missing file -> app['fcc_db'] is None")
            check(app2.get("fcc_db_path") == str(scratch2 / "absent.sqlite"),
                  "fcc.setup() still stashes the resolved path on missing file")
            result = fcc.lookup(app2, "W1AW")
            check(result["status"] == lookup_cache.STATUS_ERROR,
                  "missing-DB lookup -> STATUS_ERROR")
            check("unavailable" in result["error"].lower(),
                  f"missing-DB error mentions unavailability "
                  f"(got {result['error']!r})")
        finally:
            shutil.rmtree(scratch2, ignore_errors=True)

        app["fcc_db"].close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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


def _make_config(tmp, fcc_db_path):
    return tmp / "config.json", json.dumps({
        "host": "127.0.0.1", "port": PORT,
        "data_dir": str(tmp), "admin_password": "test-pw",
        "fcc_db_path": str(fcc_db_path),
    })


async def run_e2e(fcc_db_path, missing_db=False):
    preclean()
    tmp = Path(tempfile.mkdtemp(prefix="haml-lookup-"))
    try:
        if missing_db:
            fcc_path = tmp / "does_not_exist.sqlite"
        else:
            fcc_path = fcc_db_path
        config_path, body = _make_config(tmp, fcc_path)
        config_path.write_text(body)
        _make_minimal_event_db(tmp)

        proc = start_server(config_path)
        try:
            async with aiohttp.ClientSession() as session:
                if missing_db:
                    print("missing-DB config -> 502:")
                    status, b = await post_lookup(session, "W1AW")
                    check(status == 502,
                          f"missing-DB W1AW -> 502 (got {status})")
                    check("error" in b,
                          "missing-DB 502 body has error field")
                    check("unavailable" in b.get("error", "").lower(),
                          f"502 error mentions unavailability "
                          f"(got {b.get('error')!r})")
                    return

                # ---- cold Individual (W1AW) ----
                print("cold Individual (W1AW):")
                t0 = time.monotonic()
                status, body = await post_lookup(session, "W1AW")
                cold_ms = (time.monotonic() - t0) * 1000
                check(status == 200, f"cold W1AW -> 200 (got {status})")
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

                # ---- cold unknown call ----
                print("cold unknown call:")
                status, body = await post_lookup(session, "ZZZZZZ")
                check(status == 404, f"unknown ZZZZZZ -> 404 (got {status})")
                check("error" in body,
                      "404 body has error field")

                # ---- previous_callsign value (not in the table) ----
                print("previous_callsign value:")
                status, body = await post_lookup(session, "KG7WKU")
                check(status == 404,
                      f"previous_callsign value KG7WKU -> 404 (got {status})")
                check("error" in body,
                      "404 body has error field")

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
    print("unit: distance stamping:")
    check_distance_unit()
    print("unit: fcc adapter:")
    check_fcc_unit()

    print("\nend-to-end against the live server:")
    fixture_path = Path(tempfile.mkdtemp(prefix="haml-fcc-fixture-")) / "fcc.sqlite"
    try:
        build_fixture(fixture_path)
        await run_e2e(fixture_path, missing_db=False)
        await run_e2e(fixture_path, missing_db=True)
    finally:
        cleanup(fixture_path.parent)

    print(f"\n{checks} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
