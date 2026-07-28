#!/usr/bin/env python3
r"""
importer_pota.py - build the `pota_parks` table of lookup_data.sqlite.

Source: https://pota.app/all_parks_ext.csv  (one CSV, regenerated daily, ~9 MB)

This replaces _POTA/update_pota_parks.py, which only downloaded and verified the
CSV because the consumer read the CSV directly. Now the consumer reads the
database, so there is a build phase - but it is a straight column-for-column
load, not a transform: no geocoding, no joins, no reference data. The whole run
is a few seconds.

Phases
------
    1  cleanup       remove what a dead run stranded
    2  download      fetch the CSV, verify it, install it in downloads/
    3  build         load it into caches/pota_work.sqlite
    4  verify        duplicates, coverage, coordinate sanity
    5  publish       replace pota_parks in lookup_data.sqlite, atomically

Unlike every other importer in this project, the download is NOT reuse-forever.
Census and StatCan publish a vintage every few years; POTA regenerates this file
daily, and roughly a hundred parks are added or retired each week, so "the
latest" has to mean today's copy. downloads/all_parks_ext.csv is therefore
refetched on every run and the previous copy is only replaced once the new one
has been proved intact. `--no-download` builds from whatever is already there.

Schema
------
    pota_parks (
        reference       TEXT NOT NULL,   -- 'US-0001', unique; the POTA park id
        name            TEXT,
        active          INTEGER,         -- 1 = currently activatable, 0 = retired
        entity_id       INTEGER,         -- ARRL DXCC entity number; NULL for ~2.2k
        location_desc   TEXT,            -- 'US-ME', or 'US-KY,US-TN,US-VA' (!)
        latitude        REAL,            -- WGS84
        longitude       REAL,
        grid            TEXT)            -- 4-char Maidenhead locator

    idx_pota_parks_reference  UNIQUE (reference)
    idx_pota_parks_location   (location_desc)
    idx_pota_parks_coords     (latitude, longitude)

The columns are the CSV's columns, renamed from camelCase to the snake_case the
other tables use. The one value not stored verbatim is `grid`: POTA publishes a
6-character locator and this keeps the first 4, because `gridsquare` in
fcc_operators and ca_operators is 4-character, and one database that answers
"what grid is this" two different ways depending on the table is a trap. Nothing
is lost - latitude and longitude are stored at full precision on every row, so
the sub-square can be recomputed from them.

Extra columns appearing upstream are ignored (everything is read by header
name); a MISSING required column aborts the run, because that means the schema
changed under us.

Three things about this data that will bite a query written against it
----------------------------------------------------------------------
1.  **`location_desc` is a comma-separated LIST**, not a value. 1,516 parks
    straddle a border - Cumberland Gap NHP is 'US-KY,US-TN,US-VA'. So

        WHERE location_desc = 'US-TN'                -- misses it
        WHERE ',' || location_desc || ',' LIKE '%,US-TN,%'   -- finds it

    The second form cannot use idx_pota_parks_location and scans all ~94k rows,
    which is a few milliseconds. If that ever matters, normalise it into a
    companion table; it is deliberately not done here, because one park is one
    row and this table is the park list.

2.  **(0.0, 0.0) is POTA's "no coordinates" placeholder**, not a location -
    2,576 parks carry it (69 of them active). It is stored verbatim rather than
    turned into NULL, because that is what the source says and the guess is the
    consumer's to make. Exclude it:

        WHERE NOT (latitude = 0 AND longitude = 0)

3.  A handful of latitudes are **out of range** upstream (six as of writing, up
    to 100.856; at least one is a plain lat/lon swap). Phase 4 counts them and
    names them in a banner. They are stored as-is - "fixing" a swap is a guess,
    and a point at 92°N simply matches no bounding box, which is harmless.

This importer owns exactly the `pota_parks` table and touches nothing else in
lookup_data.sqlite.

Usage
-----
    .venv\Scripts\python importer_pota.py [--no-download]

or entry 5 of run_importers.py, which is the same thing with no flags.

Requires requests (already in requirements.txt for every other importer).
"""

import argparse
import csv
import os
import sqlite3
import sys
import time

# requests is the only third-party dependency, and this is the whole preflight:
# there is no equivalent of the boundary importer's R*Tree probe, and nothing
# else to check before the (3-second) download.
#
# The guard is on the import itself rather than in a preflight() function
# because a function could never run - the bare `import requests` would already
# have raised. SystemExit here is what the other importers' phases raise, so
# both callers report it properly: run_importers.py catches it and prints the
# message beside the importer's name, and a direct CLI run prints it and exits 1
# instead of dumping a traceback.
try:
    import requests
except ImportError:
    sys.exit("ERROR: missing required package:\n"
             "  requests\n"
             "\nInstall it:\n"
             "  python -m pip install requests\n"
             "  (or: python -m pip install -r requirements.txt)")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))

DOWNLOADS_DIR = os.path.join(HERE, "downloads")
CACHES_DIR = os.path.join(HERE, "caches")
LOGS_DIR = os.path.join(HERE, "logs")

DB_PATH = os.path.join(HERE, "lookup_data.sqlite")
WORK_DB = os.path.join(CACHES_DIR, "pota_work.sqlite")

RUN_LOG = "pota_run.log"

URL = "https://pota.app/all_parks_ext.csv"
CSV_NAME = "all_parks_ext.csv"
CSV_PATH = os.path.join(DOWNLOADS_DIR, CSV_NAME)

TABLE = "pota_parks"      # the published name
WORK_TABLE = "parks"      # the name Phases 3-4 use inside the work database

HTTP_HEADERS = {"User-Agent": "pota-import/1.0 (+lookup_data build)"}

# (30s connect, 300s read). The file is ~9 MB off a CDN and takes a couple of
# seconds; 300 is the "it is alive but crawling" bound, not an expectation.
HTTP_TIMEOUT = (30, 300)

# CSV header -> column name. This mapping IS the schema contract: every key must
# be present in the header or the run aborts, and nothing outside it is stored.
COLUMNS = {
    "reference": "reference",
    "name": "name",
    "active": "active",
    "entityId": "entity_id",
    "locationDesc": "location_desc",
    "latitude": "latitude",
    "longitude": "longitude",
    "grid": "grid",
}

# Floor for a plausible file, inherited from _POTA/update_pota_parks.py. There
# were 93,719 parks in mid-2026 and the list only grows, so anything this far
# below it is a truncated download rather than a real shrink. It exists to catch
# a half-written file whose CSV still parses - the header check catches an HTML
# error page, and this catches the connection that dropped at 40%.
MIN_ROWS = 50_000

INSERT_BATCH = 5_000

# --------------------------------------------------------------------------- #
# Logging (console + utf-8 log file)
# --------------------------------------------------------------------------- #

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
    """Log a rule-delimited block that stands out in a wall of progress lines."""
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


# --------------------------------------------------------------------------- #
# Phase 1 - cleanup
# --------------------------------------------------------------------------- #

def cleanup_old_data():
    """Delete what a previous run stranded.

    Deliberately does NOT delete lookup_data.sqlite or the installed CSV. The
    table is replaced in one transaction only once its replacement is complete,
    and the CSV is renamed into place only once verified, so a run that dies
    before either leaves both exactly as they were.
    """
    victims = [WORK_DB, WORK_DB + "-journal", CSV_PATH + ".part"]
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
    log(f"Cleanup: {removed} stale file(s) removed. The published table and the "
        f"installed {CSV_NAME} stay in place until replaced atomically.")


# --------------------------------------------------------------------------- #
# Phase 2 - download and verify the CSV
# --------------------------------------------------------------------------- #

def verify_csv(path):
    """Prove `path` is a POTA park list, not a truncated file or an error page.

    Returns the data row count. Raises ValueError on anything that would make
    the file unsafe to install over a known-good previous copy.

    This runs on the .part file, before the rename - the whole point is that a
    bad download can never become the file a later --no-download run trusts.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except (StopIteration, UnicodeDecodeError) as e:
            raise ValueError(f"unreadable as CSV ({e or 'file is empty'})")
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            # Truncate what we echo back: when the server hands us an HTML error
            # page instead of the CSV, its first "column" is the whole document.
            got = ", ".join(header)
            if len(got) > 120:
                got = got[:120] + "..."
            raise ValueError(f"header is missing {', '.join(missing)} (got: {got})")
        extra = [c for c in header if c not in COLUMNS]
        try:
            rows = sum(1 for _ in reader)
        except (csv.Error, UnicodeDecodeError) as e:
            raise ValueError(f"malformed part-way through ({e})")
    if rows < MIN_ROWS:
        raise ValueError(f"only {rows:,} rows, expected at least {MIN_ROWS:,} "
                         f"- the download looks truncated")
    if extra:
        # Not a failure: consumers read by header name, so a new upstream column
        # is additive. Worth saying out loud, because it is the signal that this
        # importer could be storing something it currently drops.
        log_banner([
            f" POTA has added {len(extra)} column(s) not stored in {TABLE}:",
            f"   {', '.join(extra)}",
            " These are ignored. Add them to COLUMNS (and the DDL) to keep them.",
        ])
    return rows


def download_csv():
    """Fetch the current CSV into downloads/, replacing the previous copy.

    Lands in a .part file and is verified before it takes the real name, so this
    path never points at a truncated file or an HTML error page that a later
    --no-download run would happily "reuse".

    Returns the row count on success, or None if the fetch failed and a usable
    previous copy is being kept instead. Raises SystemExit only when there is
    nothing to fall back to.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    tmp = CSV_PATH + ".part"
    log(f"Downloading {URL}")
    try:
        with requests.get(URL, timeout=HTTP_TIMEOUT, stream=True,
                          headers=HTTP_HEADERS) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        size = os.path.getsize(tmp)
        rows = verify_csv(tmp)
    except (requests.RequestException, ValueError, OSError) as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        if os.path.exists(CSV_PATH):
            # Same rule as the boundary importer: an unreachable or misbehaving
            # server falls back to what is already on disk rather than failing
            # the run. Loud, because the result is then not today's list.
            age = (time.time() - os.path.getmtime(CSV_PATH)) / 86400
            log_banner([
                f" DOWNLOAD FAILED: {e}",
                f" Falling back to the {CSV_NAME} already in downloads/,",
                f" last updated {age:,.1f} day(s) ago. POTA regenerates the list",
                f" daily, so the table this run publishes is NOT current.",
            ])
            return None
        sys.exit(f"ERROR: could not download the POTA park list ({e}), and there "
                 f"is no previous copy at {CSV_PATH} to fall back to. Nothing "
                 f"was changed; the previously published {TABLE} is untouched.")
    os.replace(tmp, CSV_PATH)
    log(f"  {size / 1e6:,.1f} MB, {rows:,} parks -> downloads/{CSV_NAME}")
    return rows


def use_existing_csv():
    """--no-download: verify and use the copy already in downloads/."""
    if not os.path.exists(CSV_PATH):
        sys.exit(f"ERROR: --no-download was given but there is no {CSV_PATH}. "
                 f"Run once without the flag to fetch it.")
    try:
        rows = verify_csv(CSV_PATH)
    except ValueError as e:
        sys.exit(f"ERROR: the existing {CSV_NAME} is unusable ({e}). Rerun "
                 f"without --no-download to refetch it.")
    age = (time.time() - os.path.getmtime(CSV_PATH)) / 86400
    log(f"--no-download: using downloads/{CSV_NAME} ({rows:,} parks, "
        f"{age:,.1f} day(s) old)")
    return rows


# --------------------------------------------------------------------------- #
# Phase 3 - build
#
# One schema, two names, as in the other importers: the work database and the
# published one get identical DDL, so publishing is a copy rather than a
# transform. {q} is the schema qualifier ("" or "lookup.").
#
# Each of these is ONE statement, executed individually rather than as a script:
# executescript() COMMITs any open transaction first, and the publish needs its
# drop/create/copy/index to be a single unit.
# --------------------------------------------------------------------------- #

DROP_TABLE = "DROP TABLE IF EXISTS {q}{table}"

SCHEMA = """
CREATE TABLE {q}{table} (
    reference     TEXT NOT NULL,  -- 'US-0001'; unique, see idx below
    name          TEXT,
    active        INTEGER,        -- 1 activatable, 0 retired
    entity_id     INTEGER,        -- ARRL DXCC entity number (291 = US)
    location_desc TEXT,           -- ISO-3166-2 subdivision, COMMA-SEPARATED LIST
    latitude      REAL,           -- WGS84; (0,0) is POTA's "unknown" placeholder
    longitude     REAL,
    grid          TEXT            -- Maidenhead locator, first 4 of POTA's 6
);
"""

INDEXES = (
    # Not declared as a PRIMARY KEY on the build table on purpose: a duplicate
    # would then abort the INSERT with a bare IntegrityError naming no
    # reference. Phase 4 checks for duplicates first and names the offenders,
    # and this index - created only at publish, over data already proved unique
    # - is what keeps the guarantee in the published file.
    "CREATE UNIQUE INDEX {q}idx_{table}_reference ON {table}(reference)",
    # 'parks in Maine'. Exact-match only; the multi-value rows in the module
    # docstring need the LIKE form, which cannot use this.
    "CREATE INDEX {q}idx_{table}_location ON {table}(location_desc)",
    # 'parks near here'. Leading column carries a bbox query: the latitude range
    # narrows ~94k rows to a few hundred, and the longitude test then runs off
    # the index entry rather than the table row.
    "CREATE INDEX {q}idx_{table}_coords ON {table}(latitude, longitude)",
)

INSERT = (f"INSERT INTO {WORK_TABLE} (reference, name, active, entity_id, "
          f"location_desc, latitude, longitude, grid) VALUES (?,?,?,?,?,?,?,?)")


def _text(v):
    v = (v or "").strip()
    return v or None


def _grid4(v):
    """POTA's 6-character locator -> the 4-character form the operator tables
    use.

    Truncation, not recomputation: characters 0-3 of a Maidenhead locator ARE
    its square, so slicing is exact. Applied unconditionally, including to the
    handful of locators upstream derived from out-of-range coordinates (see
    Phase 4) - those are nonsense at 6 characters and equally nonsense at 4, and
    exempting them would put the only 6-character values in the database in the
    rows least worth trusting.
    """
    v = _text(v)
    return v[:4] if v else None


def _int(v):
    v = (v or "").strip()
    try:
        return int(v)
    except ValueError:
        return None


def _float(v):
    v = (v or "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def build(con, path):
    """Load the CSV into the work database. Returns (rows read, rows stored).

    Streams and inserts in batches - the file is only ~9 MB, but there is no
    reason to hold 94k tuples to write them.
    """
    for stmt in (DROP_TABLE.format(q="", table=WORK_TABLE),
                 SCHEMA.format(q="", table=WORK_TABLE)):
        con.execute(stmt)

    read = stored = skipped = 0
    batch = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            read += 1
            ref = _text(row.get("reference"))
            if not ref:
                # A park with no reference cannot be looked up or activated; it
                # is not a park, it is a bad line.
                skipped += 1
                continue
            batch.append((
                ref,
                _text(row.get("name")),
                _int(row.get("active")),
                _int(row.get("entityId")),
                _text(row.get("locationDesc")),
                _float(row.get("latitude")),
                _float(row.get("longitude")),
                _grid4(row.get("grid")),
            ))
            if len(batch) >= INSERT_BATCH:
                con.executemany(INSERT, batch)
                stored += len(batch)
                batch.clear()
    if batch:
        con.executemany(INSERT, batch)
        stored += len(batch)
    con.commit()

    log(f"  {read:,} CSV row(s) read, {stored:,} stored")
    if skipped:
        log(f"  {skipped} row(s) skipped: no reference")
    return read, stored


# --------------------------------------------------------------------------- #
# Phase 4 - verify
# --------------------------------------------------------------------------- #

def verify_table(con):
    """Check what would otherwise be discovered by a wrong answer later.

    Returns True if the build is fit to publish. Coordinate oddities are
    reported but do NOT fail the run - they are upstream's, they are a handful
    of rows, and refusing to publish 94k good parks over six bad latitudes
    would be the wrong trade.
    """
    ok = True
    q = lambda sql: con.execute(sql).fetchone()[0]

    n = q(f"SELECT COUNT(*) FROM {WORK_TABLE}")
    if not n:
        log("  table is EMPTY")
        return False
    if n < MIN_ROWS:
        ok = False
        log(f"  only {n:,} row(s), expected at least {MIN_ROWS:,}")

    # Duplicates, before the UNIQUE index at publish time - see INDEXES.
    dups = con.execute(
        f"SELECT reference, COUNT(*) FROM {WORK_TABLE} GROUP BY reference "
        f"HAVING COUNT(*) > 1 ORDER BY 2 DESC, 1").fetchall()
    if dups:
        ok = False
        shown = ", ".join(f"{r} x{c}" for r, c in dups[:20])
        log(f"  DUPLICATE reference(s): {len(dups)} - {shown}"
            f"{' ...' if len(dups) > 20 else ''}")

    active = q(f"SELECT COUNT(*) FROM {WORK_TABLE} WHERE active = 1")
    prefixes = q(f"SELECT COUNT(DISTINCT substr(reference, 1, "
                 f"instr(reference, '-') - 1)) FROM {WORK_TABLE}")
    no_loc = q(f"SELECT COUNT(*) FROM {WORK_TABLE} WHERE location_desc IS NULL")
    multi = q(f"SELECT COUNT(*) FROM {WORK_TABLE} "
              f"WHERE location_desc LIKE '%,%'")
    no_coord = q(f"SELECT COUNT(*) FROM {WORK_TABLE} "
                 f"WHERE latitude IS NULL OR longitude IS NULL")
    null_island = q(f"SELECT COUNT(*) FROM {WORK_TABLE} "
                    f"WHERE latitude = 0 AND longitude = 0")
    log(f"  {n:,} park(s), {active:,} active, {prefixes} prefix(es)")
    log(f"  {multi:,} span more than one location_desc; {no_loc:,} have none")
    log(f"  {no_coord:,} have no coordinates; {null_island:,} sit at (0,0), "
        f"POTA's placeholder")

    # Out-of-range coordinates. Six rows upstream as of 2026-07, at least one a
    # plain lat/lon swap (PH-0128 at 121.497N). Named rather than silently
    # stored, because nothing downstream will ever complain: a point at 92N just
    # matches no bounding box.
    bad = con.execute(
        f"SELECT reference, latitude, longitude FROM {WORK_TABLE} "
        f"WHERE (latitude IS NOT NULL AND (latitude < -90 OR latitude > 90)) "
        f"   OR (longitude IS NOT NULL AND (longitude < -180 OR longitude > 180))"
        f" ORDER BY reference").fetchall()
    if bad:
        shown = [f"   {r:<10} {la}, {lo}" for r, la, lo in bad[:10]]
        log_banner([
            f" {len(bad)} park(s) have coordinates outside the valid range,",
            f" upstream in the POTA CSV. Stored as-is; they will match no",
            f" bounding box. At least one is a latitude/longitude swap.",
            *shown,
            *([f"   ... and {len(bad) - 10} more"] if len(bad) > 10 else []),
        ])
    return ok


# --------------------------------------------------------------------------- #
# Phase 5 - publish
# --------------------------------------------------------------------------- #

def publish(con, final_db):
    """Copy the finished work table into lookup_data.sqlite as TABLE.

    The whole replacement - drop the old table, create the new one, copy every
    row, build all three indexes - happens inside ONE transaction on the
    attached database, so a crash or a Ctrl-C partway through rolls back to the
    previously published table rather than leaving a half-copied one.

    Only TABLE is touched. Other importers' tables in the same file are not
    read, written, or dropped; they are only locked for the second or so the
    copy takes.
    """
    # Autocommit mode, so the only transaction is the explicit one below.
    # sqlite3's default mode opens transactions implicitly around DML, which
    # would collide with the BEGIN here (and ATTACH cannot run inside one).
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
    log(f"{'Replaced' if replacing else 'Created'} {TABLE} ({n:,} rows) in "
        f"{final_db}{' (previous version discarded)' if replacing else ''}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="importer_pota.py",
        description="Build the `pota_parks` table of lookup_data.sqlite from "
                    "the POTA park list at pota.app. Every path is fixed under "
                    "the project root.")
    p.add_argument("--no-download", action="store_true",
                   help="build from the all_parks_ext.csv already in "
                        "downloads/ instead of fetching today's. The list is "
                        "regenerated daily, so this publishes a stale table by "
                        "design - it exists for rebuilding without the network.")
    return p


def run(args=None):
    """Run the whole import.

    run_importers.py calls this directly with no arguments, which is the same
    as a flagless command-line run. Raises SystemExit on failure, which the menu
    catches and reports.
    """
    global _log_fh

    if args is None:
        args = build_parser().parse_args([])

    # Module state, reset because the menu may call run() twice in one process.
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
    log(f"=== POTA park import started ({time.strftime('%Y-%m-%d')}) ===")
    log(f"Building into {WORK_DB}")
    log(f"  -> becomes the {TABLE} table of {DB_PATH} on success")

    log("--- Phase 1: cleanup ---")
    cleanup_old_data()

    log("--- Phase 2: download the park list ---")
    if args.no_download:
        use_existing_csv()
    else:
        download_csv()

    # try/finally, not a bare close() at the end: run_importers.py runs this in
    # ITS OWN process and returns to the menu on failure, so a connection leaked
    # by a raising phase stays open for the rest of the session - and on Windows
    # an open handle makes the next run's cleanup of WORK_DB fail outright.
    con = sqlite3.connect(WORK_DB)
    try:
        con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")

        log("--- Phase 3: build ---")
        build(con, CSV_PATH)

        log("--- Phase 4: verify ---")
        if not verify_table(con):
            sys.exit(f"ERROR: build verification FAILED - aborting before "
                     f"publish. The failed build is left at {WORK_DB} for "
                     f"inspection and is NOT published; the previously "
                     f"published {TABLE} is untouched.")

        log("--- Phase 5: publish ---")
        # No VACUUM: publish() copies rows into a freshly created table, which
        # is already compact, and the work database is deleted by the next run.
        publish(con, DB_PATH)
    finally:
        con.close()

    log(f"=== SUCCESS: {TABLE} in {DB_PATH} in {time.time() - t0:,.1f} seconds ===")
    replay_notices()
    _log_fh.close()
    _log_fh = None


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user (Ctrl-C). The published table is untouched - "
            "the work database is cleaned up by the next run.")
        try:
            if _log_fh:
                _log_fh.close()
        except Exception:
            pass
        sys.exit(1)
