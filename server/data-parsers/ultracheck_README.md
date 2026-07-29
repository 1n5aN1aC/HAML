# ultracheck

A SQLite database of amateur radio callsigns built from six public sources, indexed for
**partial-callsign search**. Searching `1AZ` returns `1AZ`, `K1AZ` and `K1AZQ` — any callsign
containing that fragment — in a single query, with every source's metadata attached.

As of the last build: **304,647 callsigns**, 89 MB, typical query **under 2 ms**.

```bash
python ultracheck_update.py              # fetch all sources, merge into ultracheck.sqlite
python ultracheck_update.py --only lotw  # refresh one source only
python ultracheck_update.py --rebuild    # discard everything and start over (destructive)
```

The script only *builds* the database. Searching it is a plain SQLite read — see
[Querying](#querying) for a drop-in module.

Requires Python 3.8+ and `requests`. Nothing else — everything else is stdlib.

---

## Data sources

All six are public, unauthenticated HTTP. No API keys, no scraping, no login.

| Source | What it contributes | Column(s) | Calls |
|---|---|---|---:|
| [Club Log](https://cdn.clublog.org/clublog-users.json.zip) | membership + last QSO | `clublog`, `clublog_last_qso` | 269,828 |
| [LoTW](https://lotw.arrl.org/lotw-user-activity.csv) | last upload date | `lotw_last_upload` | 233,627 |
| [Super Check Partial](https://www.supercheckpartial.com/MASTER.SCP) | membership only | `scp` | 50,000 |
| [POTA hunters](https://api.pota.app/leaderboard/hunter) | hunter QSO count | `pota_hunter_qsos` | 32,248 |
| [POTA activators](https://api.pota.app/activator/all) | activation count | `pota_activations` | 29,551 |
| [ARRL Field Day](https://contests.arrl.org/ContestResults/) | last year entered | `fd_last_year` | 26,368 |
| [Winter Field Day](https://winterfieldday.org/queries/query_results.php) | last year entered | `wfd_last_year` | 5,115 |

Only 658 callsigns appear in all seven columns; 19.6% come from a single source. The sources
genuinely complement each other, which is why the matcher unions all of them.

### Per-source notes

**Club Log** — a 5.5 MB zip regenerated weekly. The JSON member inside is named
`clublog_users.json` (underscore) unlike the zip itself, and the payload is an *object keyed by
callsign*, not an array — the official docs say array and are wrong. Every field except the key
is optional; only ~40% of records carry `lastqso`, so `clublog = 1` means "is a member" while
`clublog_last_qso` is independently nullable. Suffixed operations appear as separate keys
(`1A0C_14`) and are folded into the base call.

**LoTW** — headerless CSV, `CALLSIGN,YYYY-MM-DD,HH:MM:SS`, regenerated weekly. Presence means
"has ever uploaded", not "is active" — filter on the date yourself if you need recency.

**SCP** — plain text, one call per line; skip blanks and lines starting with `!` or `#`. Contest
callsigns only, no dates or counts, so it contributes membership alone.

**POTA** — two undocumented endpoints found in the site's JS bundles; treat them as liable to
break without notice. Note the hunter board is **truncated server-side at 100 hunted parks**, so
a `NULL` in `pota_hunter_qsos` means "below the cutoff", not "zero". Both endpoints report
*cumulative* totals rather than deltas. The activator endpoint serves brotli that urllib3 fails
to decode, so the fetcher requests `gzip, deflate` explicitly.

**ARRL Field Day** — one CSV per year, 2010–2025 (2009 and earlier are PDF-only; the newest year
404s until ARRL publishes it, months after the event). Column names change across eras, so they
are matched by name, never position. Three encoding quirks are handled: files before 2019 are
cp1252 with a **slashed zero** (`AAØK` = `AA0K`), the 2023 sheet flattened that to `?` (`KK?D`),
and 2024 contains a double-encoded `KÃ˜LOA`. A single cell can also name several stations —
`AA4RV (+KO4HUL)`, `W4FYI (W4AMW & K4FYI)` — and each is recorded as an entrant.

**Winter Field Day** — a JSON endpoint behind the results page. `op_class` **must** be one of
`H`/`I`/`O`/`M`; passing an empty value silently returns a fraction of the rows, so all four are
queried and unioned. Only 2024+ is served. The row `timestamp` is a processing time, not the
operating date — the event year comes from the `selected_year` used in the request.

**Shift-key mangling** — both ARRL sheets and the WFD feed contain calls where a digit arrived as
its shifted character: `N&CHN` for N7CHN, `WB@UFO` for WB2UFO, `KJ^HCG` for KJ6HCG, `N(DPP` for
N9DPP. These are un-shifted on import. The repair is safe rather than a guess: those symbols
appear in zero valid rows across LoTW's 233,627 and SCP's 50,000, so undoing the shift can only
rescue a string that would otherwise be discarded.

---

## Querying

### Schema

```
callsigns(id, callsign, fd_last_year, wfd_last_year, pota_hunter_qsos,
          pota_activations, lotw_last_upload, clublog, clublog_last_qso,
          scp, source_count, first_seen, last_seen)
call_suffix(suffix, call_id)      -- every suffix of every callsign
meta(key, value)                  -- build timestamp, row counts
source_runs(source, last_run, rows_seen)
```

`clublog` and `scp` are 0/1 flags. `source_count` is how many of the seven columns are populated —
a cheap relevance signal. `NULL` means "this source doesn't know the call", never "zero".

### How partial search works

A substring search can't use a normal index — `LIKE '%1AZ%'` forces a full table scan. So
`call_suffix` stores **every suffix** of every callsign:

```
K1AZQ  ->  K1AZQ, 1AZQ, AZQ, ZQ, Q
```

A substring of the callsign is now a *prefix* of one of its suffixes, and a prefix match is a
range scan the B-tree serves directly. `idx_suffix(suffix, call_id)` covers it, so the lookup
never touches the table itself. 304,647 callsigns expand to 1,759,952 suffix rows.

### The query

One query returns every hit with all metadata. Bind `:q` as the uppercased search term,
`:qlen` as its length, and `:q_hi` as the term with `￿` appended (the range upper bound):

```sql
SELECT c.callsign,
       c.fd_last_year,
       c.wfd_last_year,
       c.pota_hunter_qsos,
       c.pota_activations,
       c.lotw_last_upload,
       c.clublog,
       c.clublog_last_qso,
       c.scp,
       c.source_count,
       CASE WHEN c.callsign = :q                   THEN 0
            WHEN substr(c.callsign, 1, :qlen) = :q THEN 1
            ELSE 2 END AS match_rank
  FROM callsigns c
 WHERE c.id IN (SELECT call_id
                  FROM call_suffix
                 WHERE suffix >= :q AND suffix < :q_hi)
 ORDER BY match_rank,
          length(c.callsign),
          c.source_count DESC,
          c.callsign
 LIMIT :limit
```

`match_rank` orders exact matches first, then calls *starting* with the term, then calls merely
containing it. Within a rank, shorter calls and better-attested calls come first.

The `IN (SELECT ...)` also de-duplicates: a call like `AAA` has three suffixes that all start
with `A`, and without it the row would come back three times.

### Using it from your own code

Nothing needs to be imported from `ultracheck_update.py` — it is a build script and pulls in
`requests`, which a reader has no use for. Open the file read-only and run the query above.
A complete query-only module:

```python
"""ultracheck_search.py -- read-only partial-callsign lookup. Stdlib only."""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "ultracheck.sqlite")

SEARCH_SQL = """
SELECT c.callsign,
       c.fd_last_year,
       c.wfd_last_year,
       c.pota_hunter_qsos,
       c.pota_activations,
       c.lotw_last_upload,
       c.clublog,
       c.clublog_last_qso,
       c.scp,
       c.source_count,
       CASE WHEN c.callsign = :q                   THEN 0
            WHEN substr(c.callsign, 1, :qlen) = :q THEN 1
            ELSE 2 END AS match_rank
  FROM callsigns c
 WHERE c.id IN (SELECT call_id
                  FROM call_suffix
                 WHERE suffix >= :q AND suffix < :q_hi)
 ORDER BY match_rank,
          length(c.callsign),
          c.source_count DESC,
          c.callsign
 LIMIT :limit
"""


def connect(db_path=DB_PATH):
    """Open the database read-only. Safe to keep open and reuse."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def search(conn, term, limit=50):
    """-> list of dicts, best match first. Empty list if nothing matches."""
    q = term.strip().upper()
    if not q:
        return []
    cur = conn.execute(SEARCH_SQL, {"q": q, "qlen": len(q),
                                    "q_hi": q + "￿", "limit": limit})
    return [dict(r) for r in cur.fetchall()]
```

```python
conn = connect()
for hit in search(conn, "1az", limit=10):
    print(hit["callsign"], hit["source_count"], hit["lotw_last_upload"])
```

Three things to get right:

- **`:q_hi` is the term plus `￿`**, the range upper bound. Every suffix starting with the
  term sorts below it, so `suffix >= :q AND suffix < :q_hi` is exactly the prefix scan.
- **Uppercase the term** before binding. Everything in the database is stored uppercase and the
  comparison is a plain byte range, not a collation.
- **Open read-only** (`mode=ro`). The connection is then safe to share across threads with
  `check_same_thread=False`, and a concurrent rebuild can't be corrupted by a reader.

Keep one connection alive for the life of the process rather than reconnecting per query —
SQLite caches the page map and the prepared statement, and reopening is most of the cost of a
sub-2 ms lookup.

If the process must survive a rebuild happening underneath it, catch `sqlite3.DatabaseError` on
a query and reconnect: `--rebuild` deletes and recreates the file, so an open handle is left
pointing at the old inode.

### Interpreting a hit

```
callsign='K1AZ' fd_last_year=2023 wfd_last_year=2025 pota_activations=26
pota_hunter_qsos=891 lotw_last_upload='2016-03-29' clublog=1
clublog_last_qso=None scp=0 source_count=6
```

Last entered Field Day in 2023 and Winter Field Day in 2025; 26 POTA activations and 891 hunter
QSOs; last LoTW upload 2016-03-29; a Club Log member with no recorded last-QSO date. Not in SCP.

---

## Refreshing

**Builds accumulate — they never delete.** Each run adds callsigns it hasn't seen and advances
the ones it has: a later year, a newer date, a higher count. A callsign already in the database
is never removed, and a populated column is never cleared, so history the upstream sources drop
(Winter Field Day only serves 2024+, POTA truncates its hunter board) survives locally once
captured.

| Column | Merge rule |
|---|---|
| `fd_last_year`, `wfd_last_year` | `max` — a later year wins, an earlier one is ignored |
| `pota_hunter_qsos`, `pota_activations` | `max` — counts only go up |
| `lotw_last_upload`, `clublog_last_qso` | `max` — newer timestamp wins |
| `clublog`, `scp` | sticky flag — once 1, never back to 0 |

POTA reports cumulative totals, so `max` is what "increment the QSO count" has to mean; adding
would double-count on every run.

A `NULL` from the current run always loses to a stored value, which makes single-source refreshes
safe:

```bash
python ultracheck_update.py --only lotw  # touches only the LoTW column
```

Runs are idempotent — building twice in a row reports `0 new callsigns`.

Downloads are cached in `caches/` with ETag and Last-Modified revalidation, so an unchanged
source returns `304 Not Modified` and costs nothing. Weekly is plenty; all six regenerate weekly
at best. `--force` ignores the cache. The `source_runs` table records when each source was last
fetched and how many rows it contributed.

## Caveats

- **Nothing is ever removed**, so a callsign whose license has lapsed stays in results forever.
  `last_seen` is the hook for fading or filtering those; nothing does it automatically.
- **Field Day entries are stations, not operators.** A 15-person club effort is one callsign.
  Per-operator data would need the Cabrillo logs, which ARRL does not publish.
- **`NULL` is not zero.** It means the source has no record — and for POTA hunters specifically,
  it may mean the call fell below the server's 100-park cutoff.
- **The POTA endpoints are undocumented** and may change or disappear without notice.
