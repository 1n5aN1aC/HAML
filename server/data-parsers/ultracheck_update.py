#!/usr/bin/env python3
"""
ultracheck_update.py -- Build a partial-callsign search database from six public
amateur-radio callsign sources.

Sources (one column group each in the `callsigns` table):

  ARRL Field Day     last year the call appeared in a FD results file
  Winter Field Day   last year the call submitted a WFD log
  POTA Hunters       number of hunter QSOs
  POTA Activators    number of activations
  LoTW               last upload date
  Club Log           last QSO timestamp
  SCP                membership only (flag)

Search is by *substring*: "1AZ" matches "1AZ", "K1AZ" and "K1AZQ".  That is
implemented with a suffix index -- every suffix of every callsign is stored in
`call_suffix`, so a substring search becomes a prefix (range) scan over a
B-tree, and one query returns every hit together with all of its metadata.

The build is *accumulative*.  Each run adds callsigns it has not seen before
and advances the ones it has -- a newer LoTW upload date, a later Field Day
year, a higher POTA QSO count.  A callsign already in the database is never
removed, and a column is never cleared, so history the upstream sources drop
(Winter Field Day only serves 2024+, POTA truncates its hunter board at 100
parks) survives locally once captured.  `--rebuild` is the only destructive
path.

Usage:
    python ultracheck_update.py              # fetch all sources, merge in
    python ultracheck_update.py --only lotw  # refresh one source only
    python ultracheck_update.py --rebuild    # discard and start over

Requires: Python 3.8+ and `requests`.  Nothing else.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "ultracheck.sqlite")
CACHE_DIR = os.path.join(HERE, "caches")

USER_AGENT = "callsign-db/1.0 (+partial-callsign lookup)"
TIMEOUT = 120
POLITE_DELAY = 0.5  # seconds between requests to the same small endpoint

FD_FIRST_YEAR = 2010          # 2009 and earlier are PDF-only
WFD_FIRST_YEAR = 2024         # nothing earlier is served
WFD_CLASSES = "HIOM"          # passing "" or 0 silently truncates (server bug)

# Alphanumeric groups joined by / or -, 3+ chars overall.  Deliberately loose:
# it has to accept plain calls (K1AZQ), DXCC prefixes (3A/DL2COM), portable
# suffixes (W1AW/4) and Club Log's dashed variants (2E0HGT-1), while still
# rejecting the junk rows the sources contain ("(NONE)", club names, headers).
CALL_RE = re.compile(r"^[A-Z0-9]+(?:[/-][A-Z0-9]+)*$")
MIN_CALL_LEN = 3

# Both ARRL sheets and the WFD feed contain calls where a digit arrived as its
# shifted character -- "N&CHN" for N7CHN, "WB@UFO" for WB2UFO, "KJ^HCG" for
# KJ6HCG.  None of these symbols can occur in a real callsign, so undoing the
# shift only ever rescues a string that would otherwise be thrown away.
UNSHIFT = str.maketrans(")!@#$%^&*(", "0123456789")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def current_year() -> int:
    return datetime.now(timezone.utc).year


# --------------------------------------------------------------------------
# HTTP with on-disk conditional caching
# --------------------------------------------------------------------------

class Fetcher:
    """GET with ETag / Last-Modified revalidation against a local cache.

    Every source in the handoffs regenerates weekly at best, and three of them
    are multi-megabyte, so a 304 should cost nothing.
    """

    def __init__(self, cache_dir: str = CACHE_DIR, force: bool = False):
        self.cache_dir = cache_dir
        self.force = force
        os.makedirs(cache_dir, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # api.pota.app serves brotli that urllib3's decoder chokes on; ask for
        # encodings the stdlib-backed path always handles.
        self.session.headers["Accept-Encoding"] = "gzip, deflate"

    def _paths(self, key: str):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
        return (os.path.join(self.cache_dir, safe),
                os.path.join(self.cache_dir, safe + ".meta.json"))

    def get(self, url: str, key: str, revalidate: bool = True) -> bytes:
        body_path, meta_path = self._paths(key)
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                meta = {}

        headers = {}
        if revalidate and not self.force and os.path.exists(body_path):
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        resp = self.session.get(url, headers=headers, timeout=TIMEOUT, stream=True)

        if resp.status_code == 304:
            resp.close()
            log(f"  304 not modified, using cache: {key}")
            with open(body_path, "rb") as fh:
                return fh.read()

        resp.raise_for_status()
        chunks = []
        for chunk in resp.iter_content(1 << 16):
            if chunk:
                chunks.append(chunk)
        data = b"".join(chunks)

        tmp = body_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, body_path)
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"url": url,
                       "etag": resp.headers.get("ETag"),
                       "last_modified": resp.headers.get("Last-Modified"),
                       "fetched": datetime.now(timezone.utc).isoformat(),
                       "bytes": len(data)}, fh, indent=2)
        log(f"  fetched {len(data):,} bytes: {key}")
        return data

    def get_optional(self, url: str, key: str):
        """Like get(), but returns None on 404 (used to probe for a new year)."""
        try:
            return self.get(url, key)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise


# --------------------------------------------------------------------------
# Callsign normalisation
# --------------------------------------------------------------------------

def clean_call(raw) -> str:
    """Return a normalised callsign, or "" if the value is not a callsign."""
    if raw is None:
        return ""
    call = str(raw).strip().upper()
    # Strip stray quoting/whitespace and the placeholder rows ARRL leaves behind.
    call = call.strip('"\'` \t')
    # ARRL's Field Day sheets write zero as a slashed zero.  "Ã˜" is that same
    # character after a round of double-encoding, which the 2024 sheet contains.
    call = call.replace("Ã˜", "0").replace("Ø", "0").replace("∅", "0")
    call = call.translate(UNSHIFT)
    if len(call) < MIN_CALL_LEN or call in {"(NONE)", "NONE", "N/A", "CALL"}:
        return ""
    if not CALL_RE.match(call):
        return ""
    return call


# --------------------------------------------------------------------------
# Source: ARRL Field Day
# --------------------------------------------------------------------------

FD_URL = "https://contests.arrl.org/ContestResults/{year}/field-day-{year}.csv"

# Splits "W4FYI  (W4AMW & K4FYI)" and "AA4RV (+KO4HUL)" into their parts.
FD_SPLIT_RE = re.compile(r"[()&+,;\s]+")


def decode_arrl(data: bytes) -> str:
    """Field Day CSVs are UTF-8 from 2019 on (with a BOM on 2025) but cp1252
    before that, where the slashed zero lives at 0xD8."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def fd_calls_in(field) -> list:
    """A Field Day call cell may name more than one station.

    Entries are recorded as `AA4RV (+KO4HUL)` or `W4FYI (W4AMW & K4FYI)` when
    calls were combined; every token in there is a real callsign that entered.
    """
    if not field:
        return []
    out = []
    # In the 2023 sheet the slashed zero was already flattened to "?" upstream
    # ("KK?D"), so inside a call cell a "?" can only be a zero.
    field = str(field).replace("?", "0")
    for token in FD_SPLIT_RE.split(field):
        call = clean_call(token)
        if call and call not in out:
            out.append(call)
    return out


def _fd_call_columns(fieldnames):
    """Field Day headers change between eras; match by name, not position.

    Returns (main_call_column, gota_call_column|None).
    """
    main = gota = None
    for name in fieldnames or []:
        if name is None:
            continue
        norm = name.replace("﻿", "").strip().upper()
        if "CALL" not in norm:
            continue
        if "GOTA" in norm:
            if gota is None:
                gota = name
        elif "CLUB" not in norm and main is None:
            # 2017 uses "CALL (does not include checklogs)".
            main = name
    return main, gota


def fetch_field_day(fetcher: Fetcher) -> dict:
    """-> {callsign: last_year_entered}"""
    out = {}
    for year in range(FD_FIRST_YEAR, current_year() + 1):
        # The newest year 404s until ARRL publishes it, which is months after
        # the event -- that is expected, not an error.
        data = fetcher.get_optional(FD_URL.format(year=year), f"fd-{year}.csv")
        if data is None:
            log(f"  Field Day {year}: not published yet (404)")
            continue
        reader = csv.DictReader(io.StringIO(decode_arrl(data), newline=""))
        main, gota = _fd_call_columns(reader.fieldnames)
        if not main:
            log(f"  Field Day {year}: no call column in {reader.fieldnames!r}, skipped")
            continue
        n = 0
        for row in reader:
            for col in (main, gota):
                if not col:
                    continue
                for call in fd_calls_in(row.get(col)):
                    n += 1
                    if out.get(call, 0) < year:
                        out[call] = year
        log(f"  Field Day {year}: {n:,} entry callsigns")
        time.sleep(POLITE_DELAY)
    log(f"Field Day: {len(out):,} unique callsigns")
    return out


# --------------------------------------------------------------------------
# Source: Winter Field Day
# --------------------------------------------------------------------------

WFD_URL = ("https://winterfieldday.org/queries/query_results.php"
           "?selected_year={year}&op_class={cls}")


def fetch_winter_field_day(fetcher: Fetcher) -> dict:
    """-> {callsign: last_year_entered}

    op_class must be one of H/I/O/M and queried separately; "all" is not a
    supported value and silently returns a fraction of the rows.
    """
    out = {}
    for year in range(WFD_FIRST_YEAR, current_year() + 1):
        year_rows = 0
        for cls in WFD_CLASSES:
            url = WFD_URL.format(year=year, cls=cls)
            data = fetcher.get(url, f"wfd-{year}-{cls}.json")
            try:
                rows = json.loads(data.decode("utf-8", errors="replace")).get("aaData") or []
            except ValueError:
                log(f"  WFD {year}/{cls}: unparseable response, skipped")
                continue
            year_rows += len(rows)
            for row in rows:
                call = clean_call(row.get("callsign"))
                if call and out.get(call, 0) < year:
                    out[call] = year
            time.sleep(POLITE_DELAY)
        log(f"  Winter Field Day {year}: {year_rows:,} entries")
    log(f"Winter Field Day: {len(out):,} unique callsigns")
    return out


# --------------------------------------------------------------------------
# Source: POTA
# --------------------------------------------------------------------------

POTA_ACTIVATOR_URL = "https://api.pota.app/activator/all"
POTA_HUNTER_URL = "https://api.pota.app/leaderboard/hunter"


def fetch_pota_activators(fetcher: Fetcher) -> dict:
    """-> {callsign: activations}"""
    data = fetcher.get(POTA_ACTIVATOR_URL, "pota-activators.json")
    rows = json.loads(data.decode("utf-8", errors="replace"))
    out = {}
    for row in rows:
        call = clean_call(row.get("activeCallsign"))
        if not call:
            continue
        n = int(row.get("activations") or 0)
        if n > out.get(call, -1):
            out[call] = n
    log(f"POTA activators: {len(out):,} callsigns")
    return out


def fetch_pota_hunters(fetcher: Fetcher) -> dict:
    """-> {callsign: qsos}

    Note: the server truncates this board at 100 hunted parks; hunters below
    that threshold are simply absent, not zero.
    """
    data = fetcher.get(POTA_HUNTER_URL, "pota-hunters.json")
    rows = json.loads(data.decode("utf-8", errors="replace"))
    out = {}
    for row in rows:
        call = clean_call(row.get("activeCallsign"))
        if not call:
            continue
        n = int(row.get("numQSOs") or 0)
        if n > out.get(call, -1):
            out[call] = n
    log(f"POTA hunters: {len(out):,} callsigns")
    return out


# --------------------------------------------------------------------------
# Source: LoTW
# --------------------------------------------------------------------------

LOTW_URL = "https://lotw.arrl.org/lotw-user-activity.csv"


def fetch_lotw(fetcher: Fetcher) -> dict:
    """-> {callsign: 'YYYY-MM-DD'} (last upload).  No header row."""
    data = fetcher.get(LOTW_URL, "lotw-user-activity.csv")
    out = {}
    for row in csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"),
                                      newline="")):
        if len(row) < 2:
            continue
        call = clean_call(row[0])
        if not call:
            continue
        date = row[1].strip()[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            continue
        if date > out.get(call, ""):
            out[call] = date
    log(f"LoTW: {len(out):,} callsigns")
    return out


# --------------------------------------------------------------------------
# Source: Club Log
# --------------------------------------------------------------------------

CLUBLOG_URL = "https://cdn.clublog.org/clublog-users.json.zip"


def fetch_clublog(fetcher: Fetcher) -> dict:
    """-> {callsign: 'YYYY-MM-DD HH:MM:SS'} (last QSO).

    The payload is a JSON *object keyed by callsign* (the docs say array and
    are wrong), the member file is `clublog_users.json` (underscore, unlike the
    zip's own name), every field but the key is optional, and suffixed
    operations appear as separate keys like `1A0C_14`.
    """
    data = fetcher.get(CLUBLOG_URL, "clublog-users.json.zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise RuntimeError(f"no JSON member in Club Log zip: {zf.namelist()}")
        with zf.open(names[0]) as fh:
            users = json.loads(fh.read().decode("utf-8", errors="replace"))

    if isinstance(users, list):  # defensive: docs claim this shape
        users = {u.get("call"): u for u in users if isinstance(u, dict)}

    out = {}
    for key, rec in users.items():
        # "1A0C_14" is a suffixed operation of 1A0C -- fold it into the base call.
        call = clean_call(str(key).split("_", 1)[0])
        if not call:
            continue
        last = (rec.get("lastqso") or "").strip() if isinstance(rec, dict) else ""
        if not re.match(r"^\d{4}-\d{2}-\d{2}", last):
            last = None
        # Presence in the file is membership; only ~40% of records carry a
        # lastqso, so the value stays NULL for the rest rather than dropping
        # the call.
        prev = out.get(call, "__missing__")
        if prev == "__missing__" or (last and (prev is None or last > prev)):
            out[call] = last
    log(f"Club Log: {len(out):,} callsigns")
    return out


# --------------------------------------------------------------------------
# Source: SCP
# --------------------------------------------------------------------------

SCP_URL = "https://www.supercheckpartial.com/MASTER.SCP"


def fetch_scp(fetcher: Fetcher) -> set:
    """-> {callsign}.  Skip blank lines and anything starting with ! or #."""
    data = fetcher.get(SCP_URL, "MASTER.SCP")
    out = set()
    for line in data.decode("ascii", errors="replace").splitlines():
        line = line.strip()
        if not line or line[0] in "!#":
            continue
        call = clean_call(line)
        if call:
            out.add(call)
    log(f"SCP: {len(out):,} callsigns")
    return out


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
PRAGMA page_size = 4096;

CREATE TABLE IF NOT EXISTS callsigns (
    id                  INTEGER PRIMARY KEY,
    -- UNIQUE (not a separate index) so the upsert has a conflict target.
    callsign            TEXT    NOT NULL UNIQUE,
    -- ARRL Field Day: last year this call appeared in a results file
    fd_last_year        INTEGER,
    -- Winter Field Day: last year this call submitted a log
    wfd_last_year       INTEGER,
    -- POTA
    pota_hunter_qsos    INTEGER,
    pota_activations    INTEGER,
    -- LoTW last upload, YYYY-MM-DD
    lotw_last_upload    TEXT,
    -- Club Log membership + last QSO, YYYY-MM-DD HH:MM:SS
    clublog             INTEGER NOT NULL DEFAULT 0,
    clublog_last_qso    TEXT,
    -- Super Check Partial membership
    scp                 INTEGER NOT NULL DEFAULT 0,
    -- how many of the seven sources know this call (cheap relevance signal)
    source_count        INTEGER NOT NULL DEFAULT 0,
    -- when this call was first seen, and when a source last confirmed it
    first_seen          TEXT,
    last_seen           TEXT
);

-- Every suffix of every callsign.  A substring search on `callsign` becomes a
-- prefix range scan here, which SQLite can serve straight from this index.
CREATE TABLE IF NOT EXISTS call_suffix (
    suffix   TEXT    NOT NULL,
    call_id  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- One row per source per run, so a stale column can be told apart from a
-- source that simply was not fetched this time.
CREATE TABLE IF NOT EXISTS source_runs (
    source     TEXT PRIMARY KEY,
    last_run   TEXT,
    rows_seen  INTEGER
);
"""

POST_INDEX = """
-- Covering index: the search never has to touch the call_suffix table itself.
CREATE INDEX IF NOT EXISTS idx_suffix ON call_suffix(suffix, call_id);
"""

# Merge rules.  Nothing is ever cleared: a NULL coming in from this run leaves
# the stored value alone, and the flags are sticky.  For the numeric and date
# columns the incoming value wins only if it is larger/newer -- POTA reports
# cumulative totals rather than deltas, so taking the max is what "increment
# the QSO count" has to mean here; adding would double-count on every run.
MERGE_COLUMNS = [
    ("fd_last_year", "max"),
    ("wfd_last_year", "max"),
    ("pota_hunter_qsos", "max"),
    ("pota_activations", "max"),
    ("lotw_last_upload", "max"),
    ("clublog", "flag"),
    ("clublog_last_qso", "max"),
    ("scp", "flag"),
]


def _merge_clause(col: str, rule: str) -> str:
    if rule == "flag":
        # 0 from a run that did not fetch this source must not clear a 1.
        return f"{col} = max(callsigns.{col}, excluded.{col})"
    # max() returns NULL if any argument is NULL, so coalesce both sides first.
    return (f"{col} = max(coalesce(excluded.{col}, callsigns.{col}),"
            f" coalesce(callsigns.{col}, excluded.{col}))")


UPSERT_SQL = """
INSERT INTO callsigns
    (callsign, fd_last_year, wfd_last_year, pota_hunter_qsos, pota_activations,
     lotw_last_upload, clublog, clublog_last_qso, scp, first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(callsign) DO UPDATE SET
    {merges},
    last_seen = excluded.last_seen
"""

def suffixes(call: str):
    """All suffixes of `call`, longest first."""
    for i in range(len(call)):
        yield call[i:]


def open_database(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open the database, creating it and its schema if it does not exist."""
    conn = sqlite3.connect(db_path)
    conn.executescript("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def update_database(sources: dict, db_path: str = DB_PATH,
                    rebuild: bool = False, record_runs: bool = True) -> None:
    """Merge `sources` into the database, accumulating rather than replacing.

    New callsigns are inserted; known ones have their columns advanced to the
    newer/larger value.  A callsign already in the database is never deleted,
    even if the source that contributed it has stopped listing it.
    """
    if rebuild and os.path.exists(db_path):
        log(f"--rebuild: discarding existing {os.path.basename(db_path)}")
        os.remove(db_path)
        for ext in ("-wal", "-shm"):
            if os.path.exists(db_path + ext):
                os.remove(db_path + ext)

    conn = open_database(db_path)
    existing = conn.execute("SELECT count(*) FROM callsigns").fetchone()[0]
    log(f"Database holds {existing:,} callsigns before this run")

    fd = sources.get("fd") or {}
    wfd = sources.get("wfd") or {}
    hunters = sources.get("pota_hunters") or {}
    activators = sources.get("pota_activators") or {}
    lotw = sources.get("lotw") or {}
    clublog = sources.get("clublog") or {}
    scp = sources.get("scp") or set()

    all_calls = set()
    for d in (fd, wfd, hunters, activators, lotw, clublog):
        all_calls.update(d)
    all_calls.update(scp)
    all_calls.discard("")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"Merging {len(all_calls):,} callsigns from this run")

    def rows():
        for call in sorted(all_calls):
            yield (call,
                   fd.get(call), wfd.get(call),
                   hunters.get(call), activators.get(call),
                   lotw.get(call),
                   1 if call in clublog else 0, clublog.get(call),
                   1 if call in scp else 0,
                   now, now)

    # New rows get ids above this watermark, which is how the suffix table is
    # extended without rebuilding it.
    high_water = conn.execute(
        "SELECT coalesce(max(id), 0) FROM callsigns").fetchone()[0]

    sql = UPSERT_SQL.format(
        merges=",\n    ".join(_merge_clause(c, r) for c, r in MERGE_COLUMNS))
    conn.executemany(sql, rows())
    conn.commit()

    added = conn.execute("SELECT count(*) FROM callsigns WHERE id > ?",
                         (high_water,)).fetchone()[0]
    total_calls = conn.execute("SELECT count(*) FROM callsigns").fetchone()[0]
    log(f"{added:,} new callsigns, {total_calls - added:,} updated in place")

    # Suffixes only for the calls that did not exist before.
    write = conn.cursor()
    batch = []
    suffix_added = 0
    for call_id, call in conn.execute(
            "SELECT id, callsign FROM callsigns WHERE id > ? ORDER BY id",
            (high_water,)):
        for sfx in suffixes(call):
            batch.append((sfx, call_id))
        if len(batch) >= 100_000:
            write.executemany("INSERT INTO call_suffix VALUES (?,?)", batch)
            suffix_added += len(batch)
            batch.clear()
    if batch:
        write.executemany("INSERT INTO call_suffix VALUES (?,?)", batch)
        suffix_added += len(batch)
    conn.commit()
    log(f"{suffix_added:,} suffix rows added")

    conn.executescript(POST_INDEX)

    # source_count is derived, so recompute it over the whole table -- a call
    # can gain a source in any run.
    conn.execute("""
        UPDATE callsigns SET source_count =
            (fd_last_year     IS NOT NULL) + (wfd_last_year    IS NOT NULL)
          + (pota_hunter_qsos IS NOT NULL) + (pota_activations IS NOT NULL)
          + (lotw_last_upload IS NOT NULL) + clublog + scp
    """)

    if record_runs:
        conn.executemany(
            "INSERT OR REPLACE INTO source_runs VALUES (?,?,?)",
            [(name, now, len(sources[key]))
             for name, (key, _) in FETCHERS.items() if key in sources])
    suffix_total = conn.execute("SELECT count(*) FROM call_suffix").fetchone()[0]
    conn.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?,?)",
        [("built_utc", now),
         ("callsigns", str(total_calls)),
         ("suffix_rows", str(suffix_total))])
    conn.commit()

    log("Analyzing")
    conn.execute("ANALYZE")
    conn.commit()
    if rebuild or not existing:
        conn.execute("VACUUM")
    # WAL is worth it during the write-heavy build, but leaves -wal/-shm files
    # beside the database.  Switching back checkpoints into the main file and
    # deletes both, so what ships is a single self-contained .sqlite.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()

    size = os.path.getsize(db_path)
    log(f"{db_path} now holds {total_calls:,} callsigns ({size / 1e6:.1f} MB)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

FETCHERS = {
    "fd": ("fd", fetch_field_day),
    "wfd": ("wfd", fetch_winter_field_day),
    "pota-hunters": ("pota_hunters", fetch_pota_hunters),
    "pota-activators": ("pota_activators", fetch_pota_activators),
    "lotw": ("lotw", fetch_lotw),
    "clublog": ("clublog", fetch_clublog),
    "scp": ("scp", fetch_scp),
}


def build(args) -> int:
    names = args.only or list(FETCHERS)
    unknown = [n for n in names if n not in FETCHERS]
    if unknown:
        print(f"unknown source(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(FETCHERS)}", file=sys.stderr)
        return 2

    fetcher = Fetcher(force=args.force)
    sources = {}
    failed = []
    for name in names:
        key, fn = FETCHERS[name]
        log(f"=== {name} ===")
        try:
            sources[key] = fn(fetcher)
        except Exception as exc:  # one bad source shouldn't lose the other six
            failed.append((name, exc))
            log(f"  FAILED: {exc!r}")

    if not sources:
        print("no sources fetched, leaving the database untouched",
              file=sys.stderr)
        return 1
    update_database(sources, args.db, rebuild=args.rebuild)
    if failed:
        print("\nSources that failed (DB built without them):", file=sys.stderr)
        for name, exc in failed:
            print(f"  {name}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="With no options, every source is downloaded and merged in. "
               "Accumulative: adds new callsigns and advances existing ones. "
               "Never deletes a callsign already in the database.")
    p.add_argument("--db", default=DB_PATH, help="database path")
    p.add_argument("--only", nargs="+", metavar="SOURCE",
                   help=f"subset of: {', '.join(FETCHERS)}. Safe to use -- "
                        f"other sources' columns are left untouched.")
    p.add_argument("--force", action="store_true",
                   help="ignore the HTTP cache and re-download everything")
    p.add_argument("--rebuild", action="store_true",
                   help="delete the database and start over (destructive)")
    return build(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
