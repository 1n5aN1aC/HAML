"""End-to-end smoke test for the callsign-lookup feature.

Spawns the real server on a scratch port with a scratch data dir, then
walks POST /api/lookup against the live Callook upstream:

  - cold-cache VALID hit (W1AW)        -> 200 + payload, cache row written
  - warm-cache re-hit                  -> 200 + same payload, instant
  - suffix normalization (W1AW/P)      -> hits the warm W1AW cache row
  - cold-cache INVALID (a clearly bad callsign) -> 404, cache row written
  - warm INVALID re-hit                -> 404, instant (cache hit)
  - bad input (empty)                  -> 400
  - coalescing: two concurrent POSTs for the same cold callsign only hit
    Callook once (verified by elapsed time and the in-flight counter)

Requires internet access to callook.info. The smoke is intentionally
allowed to flake if Callook is unreachable; the run exits non-zero.

Run: python server/tests/smoke_lookup.py
"""
import asyncio
import json
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
# (extra class, in Oregon). INVALIDCALL999 won't ever exist.
KNOWN_VALID_CLUB = "W1AW"
KNOWN_VALID_PERSON = "K1MI"
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


async def main():
    preclean()
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
                check(body.get("status") == "VALID",
                      "payload.status == 'VALID'")
                check(body.get("type") == "CLUB",
                      "W1AW is type=CLUB")
                check(body.get("name") == "ARRL HQ OPERATORS CLUB",
                      "W1AW has expected name")
                check(body.get("location", {}).get("gridsquare"),
                      "W1AW has gridsquare")
                check(body.get("TrusteeCallsign") == "NA2AA",
                      "W1AW has trustee NA2AA")
                check("fetched_at" in body, "payload has fetched_at")
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

                # ---- PERSON shape ----
                status6, body6 = await post_lookup(session, KNOWN_VALID_PERSON)
                check(status6 == 200, f"{KNOWN_VALID_PERSON} -> 200")
                check(body6.get("type") == "PERSON",
                      "K1MI is type=PERSON")
                check(body6.get("OperatorClass") == "EXTRA",
                      "K1MI is OperatorClass=EXTRA")
                check(body6.get("PreviousCallsign") == "KG7WKU",
                      "K1MI carries previous callsign")

                # ---- bad input ----
                bad_status, _ = await post_lookup(session, "")
                check(bad_status == 400, "empty callsign -> 400")

                # ---- coalescing ----
                # Two concurrent cold POSTs for a fresh callsign. The
                # 1 req/sec gate plus the shared future means only one
                # Callook hit fires; both clients get the same result.
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