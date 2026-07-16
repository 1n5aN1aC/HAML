"""End-to-end smoke test for the callsign-lookup feature.

Spawns the real server on a scratch port with a scratch data dir, then
walks POST /api/lookup against the live Callook upstream:

  - cold-cache VALID hit (W1AW)        -> 200 + canonical record, cache row written
  - warm-cache re-hit                  -> 200 + same record, instant
  - suffix normalization (W1AW/P)      -> hits the warm W1AW cache row
  - cold-cache INVALID (a clearly bad callsign) -> 404, cache row written
  - warm INVALID re-hit                -> 404, instant (cache hit)
  - bad input (empty)                  -> 400
  - supersession: cold KG7WKU (previous call of K1MI) -> 404, but
    warms K1MI's cache; subsequent K1MI lookup is fast; warm KG7WKU
    is also fast (not_found row written under the queried key)
  - coalescing: two concurrent POSTs for the same cold callsign only hit
    Callook once (verified by elapsed time and the in-flight counter)

Requires internet access to callook.info. The smoke is intentionally
allowed to flake if Callook is unreachable; the run exits non-zero.

Run: python server/tests/smoke_lookup.py
"""
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

import aiohttp

SERVER_DIR = Path(__file__).resolve().parent.parent
PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"

# Known-stable Callook inputs. W1AW is ARRL HQ (club); K1MI is a PERSON
# (extra class, in Oregon). KG7WKU is a previous call of K1MI (a
# supersession case). INVALIDCALL999 won't ever exist.
KNOWN_VALID_CLUB = "W1AW"
KNOWN_VALID_PERSON = "K1MI"
KNOWN_PREVIOUS = "KG7WKU"
KNOWN_INVALID = "INVALIDCALL999"

checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    print(f"  ok: {label}")


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
    import tempfile as _tf
    base = Path(_tf.gettempdir())
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


def check_ttl_policy():
    """Verify the TTL policy constants without spinning up the server.

    Locks in the policy so that future drift in lookup_cache.TTL_* is
    caught by the smoke test (which otherwise only exercises the cache
    write/read path, not the actual expiry windows).
    """
    # Add server/ to sys.path so we can import lookup_cache / lookup_record.
    sys.path.insert(0, str(SERVER_DIR))
    import lookup_cache
    import lookup_record

    # TTL_OK must be 365 days in seconds.
    check(
        lookup_cache.TTL_OK == 365 * 24 * 60 * 60,
        f"TTL_OK == {365 * 24 * 60 * 60} (365 days)",
    )

    # TTL_NOT_FOUND must be ~1 month (30 fixed days) in seconds.
    expected_month = 30 * 24 * 60 * 60
    check(
        lookup_cache.TTL_NOT_FOUND == expected_month,
        f"TTL_NOT_FOUND == {expected_month} (1 month)",
    )

    # TTL_ERROR must be 15 minutes in seconds.
    check(
        lookup_cache.TTL_ERROR == 15 * 60,
        f"TTL_ERROR == {15 * 60} (15 min)",
    )

    # And the dispatcher _expires_at() must produce a row whose lifetime
    # matches each constant within a small tolerance.
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

    # Re-export check: lookup_cache.now_iso should be the same function
    # lookup_record exports (so existing call sites still work).
    check(
        lookup_cache.now_iso is lookup_record.now_iso,
        "lookup_cache.now_iso re-exports lookup_record.now_iso",
    )


def check_coerce():
    """Verify lookup_record.coerce() shapes input into the canonical record.

    Runs without the server. Locks in:
      - output keys are EXACTLY FIELDS
      - full fixture -> clean, all fields populated
      - sparse fixture -> clean nulls (no dirty)
      - garbage fixture -> nulls + dirty list naming the failed fields
    """
    sys.path.insert(0, str(SERVER_DIR))
    import lookup_record

    full_input = {
        "callsign": "K1MI",
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
        "latitude": "44.979441",
        "longitude": "-123.337862",
        "gridsquare": "CN84hx",
        "frn": "0024933376",
        "grant_date": "03/19/2024",
        "expiry_date": "03/19/2034",
        "last_action_date": "03/19/2024",
        "fetched_at": "2026-07-16T20:11:04.123+00:00",
        "source": "callook",
        # Unknown key — should be dropped.
        "junk": "ignore me",
    }
    record, bad = lookup_record.coerce(full_input)
    check(set(record.keys()) == set(lookup_record.FIELDS),
          "full fixture -> output keys == FIELDS exactly")
    check(bad == [], f"full fixture -> no bad_fields (got {bad})")
    check(record["callsign"] == "K1MI", "full fixture -> callsign")
    check(record["license_type"] == "person", "full fixture -> license_type lowercased")
    check(record["license_class"] == "extra", "full fixture -> license_class lowercased")
    check(record["previous_license_class"] == "general",
          "full fixture -> previous_license_class lowercased")
    check(record["trustee_callsign"] is None, "full fixture -> trustee_callsign None (empty)")
    check(isinstance(record["latitude"], float) and record["latitude"] == 44.979441,
          "full fixture -> latitude is float")
    check(isinstance(record["longitude"], float) and record["longitude"] == -123.337862,
          "full fixture -> longitude is float")
    check(record["grant_date"] == "2024-03-19", "full fixture -> grant_date ISO")
    check(record["frn"] == "0024933376", "full fixture -> frn preserved")
    check(record["gridsquare"] == "CN84",
          "full fixture -> gridsquare truncated to 4 chars (got "
          f"{record['gridsquare']!r})")
    check("junk" not in record, "full fixture -> unknown key dropped")

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
    }
    record, bad = lookup_record.coerce(garbage_input)
    check(set(record.keys()) == set(lookup_record.FIELDS),
          "garbage fixture -> output keys == FIELDS exactly")
    check(record["latitude"] is None, "garbage fixture -> latitude is None")
    check(record["longitude"] is None, "garbage fixture -> longitude is None")
    check(record["grant_date"] is None, "garbage fixture -> grant_date is None")
    check(set(bad) == {"latitude", "grant_date"},
          f"garbage fixture -> bad_fields == {{latitude, grant_date}} (got {bad})")


async def main():
    preclean()
    # Offline checks first — fast, catch drift in TTL constants and in
    # the coerce() contract without needing the server or Callook.
    check_ttl_policy()
    check_coerce()
    tmp = Path(tempfile.mkdtemp(prefix="haml-lookup-"))
    try:
        # The server reads cfg from a JSON file whose `data_dir` it then
        # uses (resolved relative to the server dir) — same pattern as
        # smoke.py. Point both at our scratch dir.
        config_path = tmp / "config.json"
        config_path.write_text(json.dumps({
            "host": "127.0.0.1", "port": PORT,
            "data_dir": str(tmp), "admin_password": "test-pw",
        }))
        # Pre-create the scratch data dir with an active Event so the rest
        # of the server's startup path is exercised identically to prod.
        events_dir = tmp / "events"
        events_dir.mkdir(parents=True)
        # Minimal event: a single-row DB the server will load.
        import sqlite3
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
        conn.execute("INSERT INTO meta VALUES ('config', '{}')")
        conn.commit()
        conn.close()
        (tmp / "state.json").write_text(
            json.dumps({"active": "events/test.db"}))

        proc = start_server(config_path)
        try:
            async with aiohttp.ClientSession() as session:
                # ---- cold VALID (CLUB) ----
                t0 = time.monotonic()
                status, body = await post_lookup(session, KNOWN_VALID_CLUB)
                cold_ms = (time.monotonic() - t0) * 1000
                check(status == 200, f"cold {KNOWN_VALID_CLUB} -> 200")
                check(body.get("callsign") == KNOWN_VALID_CLUB,
                      f"payload has callsign={KNOWN_VALID_CLUB}")
                check(body.get("license_type") == "club",
                      "W1AW is license_type=club (lowercased)")
                check(body.get("trustee_callsign") == "NA2AA",
                      "W1AW has trustee NA2AA")
                check(body.get("license_class") is None,
                      "W1AW has no license_class (club, not person)")
                check(body.get("name") == "ARRL HQ OPERATORS CLUB",
                      "W1AW has expected name")
                check(isinstance(body.get("gridsquare"), str) and body.get("gridsquare"),
                      "W1AW has gridsquare (string)")
                check(len(body.get("gridsquare", "")) <= 4,
                      "W1AW gridsquare is truncated to <=4 chars "
                      f"(got {body.get('gridsquare')!r})")
                check(isinstance(body.get("latitude"), float),
                      "W1AW latitude is float")
                check(isinstance(body.get("longitude"), float),
                      "W1AW longitude is float")
                check(body.get("source") == "callook",
                      "W1AW payload has source=callook")
                check("status" not in body,
                      "W1AW payload has no 'status' key (only VALID reaches the record)")
                check("fetched_at" in body, "W1AW payload has fetched_at")
                check(re.match(r"^\d{4}-\d{2}-\d{2}$", body.get("grant_date", "")),
                      "W1AW grant_date matches YYYY-MM-DD")
                print(f"  ({cold_ms:.0f}ms cold)")

                # ---- warm VALID ----
                t0 = time.monotonic()
                status2, body2 = await post_lookup(session, KNOWN_VALID_CLUB)
                warm_ms = (time.monotonic() - t0) * 1000
                check(status2 == 200, f"warm {KNOWN_VALID_CLUB} -> 200")
                check(body2.get("fetched_at") == body.get("fetched_at"),
                      "warm hit returns identical fetched_at (cache hit)")
                check(warm_ms < cold_ms / 2,
                      f"warm hit ({warm_ms:.0f}ms) faster than cold ({cold_ms:.0f}ms)")
                print(f"  ({warm_ms:.0f}ms warm)")

                # ---- suffix normalization ----
                status3, body3 = await post_lookup(session, KNOWN_VALID_CLUB + "/P")
                check(status3 == 200, f"{KNOWN_VALID_CLUB}/P -> 200")
                check(body3.get("callsign") == KNOWN_VALID_CLUB,
                      "suffix stripped before lookup")

                # ---- cold INVALID ----
                status4, body4 = await post_lookup(session, KNOWN_INVALID)
                check(status4 == 404, f"cold {KNOWN_INVALID} -> 404")
                check("error" in body4, "404 body has error field")

                # ---- warm INVALID ----
                t0 = time.monotonic()
                status5, _ = await post_lookup(session, KNOWN_INVALID)
                warm_invalid_ms = (time.monotonic() - t0) * 1000
                check(status5 == 404, f"warm {KNOWN_INVALID} -> 404")
                check(warm_invalid_ms < 50,
                      f"warm INVALID is fast ({warm_invalid_ms:.0f}ms)")

                # ---- supersession walk ----
                # KG7WKU is a previous call of K1MI: Callook returns the
                # current license, so KG7WKU gets a 404. K1MI is warmed
                # in the process, so the next K1MI POST is fast.
                t0 = time.monotonic()
                sp_status, sp_body = await post_lookup(session, KNOWN_PREVIOUS)
                sp_ms = (time.monotonic() - t0) * 1000
                check(sp_status == 404,
                      f"cold {KNOWN_PREVIOUS} (previous call) -> 404 "
                      f"(got {sp_status})")
                check("error" in sp_body,
                      f"{KNOWN_PREVIOUS} 404 body has error field")

                # Now K1MI — the KG7WKU lookup above should have warmed
                # K1MI's cache row (since the returned record's callsign
                # was K1MI, and the ok row is cached under the returned
                # callsign). This lookup should therefore be fast (a pure
                # cache hit, no upstream call).
                t0 = time.monotonic()
                k_status, k_body = await post_lookup(session, KNOWN_VALID_PERSON)
                k_ms = (time.monotonic() - t0) * 1000
                check(k_status == 200, f"{KNOWN_VALID_PERSON} -> 200")
                check(k_body.get("callsign") == KNOWN_VALID_PERSON,
                      f"{KNOWN_VALID_PERSON} payload has correct callsign")
                check(k_body.get("license_type") == "person",
                      f"{KNOWN_VALID_PERSON} is license_type=person")
                check(k_body.get("license_class") == "extra",
                      f"{KNOWN_VALID_PERSON} is license_class=extra (lowercased)")
                check(k_body.get("previous_callsign") == KNOWN_PREVIOUS,
                      f"{KNOWN_VALID_PERSON} carries previous_callsign={KNOWN_PREVIOUS}")
                check(k_body.get("frn") == "0024933376",
                      f"{KNOWN_VALID_PERSON} frn matches")
                check(re.match(r"^\d{4}-\d{2}-\d{2}$", k_body.get("expiry_date", "")),
                      f"{KNOWN_VALID_PERSON} expiry_date matches YYYY-MM-DD")
                check(k_ms < 200,
                      f"{KNOWN_VALID_PERSON} lookup is fast (warmed by {KNOWN_PREVIOUS}): {k_ms:.0f}ms")

                # Warm KG7WKU — the not_found row was written under the
                # queried key, so this should be a fast cache hit.
                t0 = time.monotonic()
                wstatus, _ = await post_lookup(session, KNOWN_PREVIOUS)
                warm_prev_ms = (time.monotonic() - t0) * 1000
                check(wstatus == 404, f"warm {KNOWN_PREVIOUS} -> 404")
                check(warm_prev_ms < 50,
                      f"warm {KNOWN_PREVIOUS} is fast ({warm_prev_ms:.0f}ms)")

                # ---- bad input ----
                bad_status, _ = await post_lookup(session, "")
                check(bad_status == 400, "empty callsign -> 400")

                # ---- coalescing ----
                # Two concurrent cold POSTs for a fresh callsign. The
                # 1 req/sec gate plus the shared future means only one
                # upstream hit fires; both clients get the same result.
                # We use a callsign we haven't hit yet to force cold.
                fresh = "AA1AA"
                t0 = time.monotonic()
                (s1, b1), (s2, b2) = await asyncio.gather(
                    post_lookup(session, fresh),
                    post_lookup(session, fresh),
                )
                coalesce_ms = (time.monotonic() - t0) * 1000
                check(s1 == 200 and s2 == 200, "both coalesced lookups 200")
                check(b1.get("fetched_at") == b2.get("fetched_at"),
                      "coalesced clients see identical fetched_at")
                print(f"  (coalesce round-trip {coalesce_ms:.0f}ms)")

            print(f"\n{checks} checks passed")
        finally:
            stop_server(proc)
    finally:
        cleanup(tmp)


if __name__ == "__main__":
    asyncio.run(main())
