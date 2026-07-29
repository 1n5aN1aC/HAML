"""ultracheck: partial-callsign search over seven contest/activity sources.

This is NOT a lookup source and is deliberately not in `lookup.SOURCES`. That
chain answers the question "who holds this callsign?" — first OK wins, the rest
never run, and the winner's record is what gets cached. ultracheck answers a
different question — "which known contest/activity callsigns contain this
fragment?" — so it is additive, never authoritative, and must run on every
lookup regardless of what the chain decided. It hangs off
`lookup_postprocess.apply()` instead, next to `distance`: request-time data,
never frozen into a cache row (its own DB refreshes on the upstreams' cadences,
so a cached copy would go stale independently of the row holding it).

Its answer is one JSON object under the response's `ultracheck` key. Callers
add it and move on; nothing else in the server reads it.

--- how the search works ---------------------------------------------------

A substring match can't use a normal index (`LIKE '%1AZ%'` is a table scan), so
`ultracheck.sqlite` carries a `call_suffix` table holding EVERY suffix of every
callsign: K1AZQ -> K1AZQ, 1AZQ, AZQ, ZQ, Q. A substring of a callsign is
therefore a *prefix* of one of its suffixes, and a prefix match is a B-tree
range scan — `suffix >= :q AND suffix < :q + '\\uffff'`. `idx_suffix(suffix,
call_id)` covers it. See `data-parsers/ultracheck_README.md` for the build side.

One query per source rather than one query filtered seven ways: each source
wants its own ORDER BY, and seven indexed queries with small LIMITs let SQLite
sort in C with a bounded heap instead of us pulling the whole candidate set
into Python and sorting it seven times.

Cost scales with how many callsigns contain the fragment, which falls off a
cliff as the term grows (measured against the 304k-callsign build, all seven
queries per term): 1 char ~170-270ms, 2 chars ~2.6ms typical and ~28ms worst,
3 chars ~0.3ms typical and ~1.2ms worst, 4+ chars well under 1ms. The client
only ever sends 2+ characters (`isPlausibleCallsign`), which keeps this in the
same ballpark as the offline licensee-DB reads the chain already does
synchronously. Worth remembering if a caller ever starts sending single
characters: that one case is ~1000x the typical query and would stall the
event loop, since sqlite reads here are blocking like everywhere else.

Never raises. A missing or corrupt DB leaves `app["ultracheck_db"]` as None and
every response says `available: false` with empty match lists — the same
warn-and-degrade contract as `lookup_db`, because a partial-callsign nicety must
never be able to break a lookup.
"""
import os
import sqlite3


# --- config ----------------------------------------------------------------
# How many matches each source contributes to one response. Independent per
# source because they're independently useful: a Field Day operator cares about
# the FD list and may not care that a call is in SCP. Raising these costs
# response size, not query time — the LIMIT is applied by the same bounded sort
# either way, and every source is capped even when thousands of calls match.
SOURCE_LIMITS = {
    "fd":             6,   # ARRL Field Day entrants
    "wfd":            6,   # Winter Field Day entrants
    "pota_hunter":    8,   # POTA hunters
    "pota_activator": 8,   # POTA activators
    "lotw":           8,   # Logbook of the World uploaders
    "clublog":        8,   # Club Log members
    "scp":            8,   # Super Check Partial (contest calls)
}

# --- per-source query specs -------------------------------------------------
# `value` is the column whose value rides along with each match (what that
# source knows about the call, and what it is sorted by); None for a
# membership-only source. `where` restricts the candidate set to calls this
# source actually knows — a NULL means "this source has never heard of the
# call", never "zero". `order` is the source's own relevance, applied after the
# exact match has been forced to the top.
_SOURCES = {
    # Most recent event year first: someone who entered in 2026 is a better
    # guess than someone who last entered in 2011.
    "fd": {
        "value": "fd_last_year",
        "where": "c.fd_last_year IS NOT NULL",
        "order": "c.fd_last_year DESC",
    },
    "wfd": {
        "value": "wfd_last_year",
        "where": "c.wfd_last_year IS NOT NULL",
        "order": "c.wfd_last_year DESC",
    },
    # Busiest first. Both POTA counts are cumulative totals, not deltas, so a
    # big number means a long history rather than recent activity. Note the
    # hunter board is truncated upstream at 100 hunted parks, so a NULL here
    # means "below POTA's own cutoff", not "no hunting".
    "pota_hunter": {
        "value": "pota_hunter_qsos",
        "where": "c.pota_hunter_qsos IS NOT NULL",
        "order": "c.pota_hunter_qsos DESC",
    },
    "pota_activator": {
        "value": "pota_activations",
        "where": "c.pota_activations IS NOT NULL",
        "order": "c.pota_activations DESC",
    },
    # Most recent upload first. The column is 'YYYY-MM-DD' text, which sorts
    # lexicographically in exactly chronological order, so no date parsing.
    "lotw": {
        "value": "lotw_last_upload",
        "where": "c.lotw_last_upload IS NOT NULL",
        "order": "c.lotw_last_upload DESC",
    },
    # Membership is the filter, last QSO is the sort. Only ~40% of Club Log
    # records carry a last-QSO date, so `clublog = 1` with a NULL date is
    # normal and must not be dropped — those calls sort last, behind everyone
    # with a known date. The explicit IS NULL term does that; SQLite's DESC
    # would happen to put NULLs last too, but relying on collation trivia to
    # get it right is how it silently flips later.
    "clublog": {
        "value": "clublog_last_qso",
        "where": "c.clublog = 1",
        "order": "c.clublog_last_qso IS NULL, c.clublog_last_qso DESC",
    },
    # SCP is membership only — no dates, no counts, nothing to sort by. So sort
    # by what the fragment itself implies: shortest call first (a 4-character
    # contest call is a likelier match for '1AZ' than a 6-character one), then
    # by how many of the seven sources know the call, which is the DB's own
    # cheap relevance signal.
    "scp": {
        "value": None,
        "where": "c.scp = 1",
        "order": "length(c.callsign), c.source_count DESC",
    },
}

# `c.callsign <> :q` is the exact-match-first term: it evaluates to 0 for the
# exact match and 1 for everything else, so ordering by it ascending puts an
# exact hit at the top of every source's list before that source's own sort
# applies. Trailing `c.callsign` makes the order total, so equal-ranked rows
# come back in a stable order instead of whatever the scan happened to yield.
#
# The `IN (SELECT ...)` both drives the range scan and de-duplicates: 'AAA' has
# three suffixes that all start with 'A', and without it that row would come
# back three times.
_SQL = """
SELECT c.callsign{value_column}
  FROM callsigns c
 WHERE c.id IN (SELECT call_id
                  FROM call_suffix
                 WHERE suffix >= :q AND suffix < :q_hi)
   AND {where}
 ORDER BY c.callsign <> :q,
          {order},
          c.callsign
 LIMIT :limit
"""

# Built once at import: the SQL per source never varies (the term and the limit
# are bound parameters), so there's no reason to re-format it per request.
_QUERIES = {
    name: _SQL.format(
        value_column=f", c.{spec['value']} AS value" if spec["value"] else "",
        where=spec["where"],
        order=spec["order"],
    )
    for name, spec in _SOURCES.items()
}

# Upper bound for the prefix range scan. U+FFFF sorts above every character
# that can appear in a callsign, so `suffix < term + '￿'` is every suffix
# starting with `term` and nothing else.
_RANGE_END = "￿"

# --- response shape ---------------------------------------------------------
# Always the same keys, whatever happened: every source present, `matches` a
# possibly-empty list, so a caller renders the same way for a hit, a miss, and
# a server with no ultracheck DB at all. `available` is False only when the DB
# itself is unusable — that is a broken install, not an empty result, and the
# difference is worth being able to see from the client.
def _empty(term, available=False):
    return {
        "query": term,
        "available": available,
        "sources": {
            name: {"matches": [], "truncated": False} for name in _SOURCES
        },
    }

# --- public API -------------------------------------------------------------

def search(app, callsign):
    """Partial-callsign search. -> the `ultracheck` response object.

    `callsign` is matched as a SUBSTRING, so a complete callsign, a fragment,
    or a half-typed call all work the same way. Each source contributes up to
    its own `SOURCE_LIMITS` entry, exact match first, then that source's own
    ordering. `truncated` says the source had more to give than its limit
    allowed, so a caller can say "5 of many" honestly.

    Never raises: a missing DB or a failing query degrades to
    `available: false` with empty lists.
    """
    term = (callsign or "").strip().upper()
    conn = app.get("ultracheck_db")
    # No DB, or nothing to search for. An empty term would range-scan the whole
    # suffix table for no useful answer; the callers upstream already reject an
    # empty callsign with a 400, so this is belt-and-braces.
    if conn is None or not term:
        return _empty(term, available=conn is not None)

    params = {"q": term, "q_hi": term + _RANGE_END}
    sources = {}
    try:
        for name, sql in _QUERIES.items():
            limit = SOURCE_LIMITS.get(name, 0)
            # One row over the limit: if it comes back, there was more than the
            # limit allowed, which is `truncated` without paying for a
            # COUNT(*) over the whole candidate set.
            rows = conn.execute(
                sql, dict(params, limit=limit + 1)
            ).fetchall() if limit > 0 else []
            truncated = len(rows) > limit
            sources[name] = {
                "matches": [
                    # A membership-only source (SCP) selects no value column,
                    # so the row has no `value` key at all and indexing it
                    # would raise; the spec check short-circuits that and
                    # writes an explicit None, keeping the wire shape uniform.
                    {"callsign": row["callsign"],
                     "value": row["value"] if _SOURCES[name]["value"] else None}
                    for row in rows[:limit]
                ],
                "truncated": truncated,
            }
    except sqlite3.Error as exc:
        # A readable file that fails mid-query is a corrupt or truncated build.
        # Report it as unavailable rather than returning the sources that
        # happened to answer before the failure — a half-filled result would
        # read as "these are the matches" when it isn't.
        print(f"warning: ultracheck query for {term!r} failed ({exc}); "
              "reporting ultracheck as unavailable")
        return _empty(term)

    return {"query": term, "available": True, "sources": sources}

# --- lifecycle --------------------------------------------------------------
# Own connection, not lookup_db's: this is a separate sqlite file on a separate
# refresh cadence. Read-only via a file: URI, the same way lookup_db opens the
# licensee datasets.
def _open(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # Row factory so the query builder can select columns by name; ordinal
    # tuples would break the moment a spec above grows a column.
    conn.row_factory = sqlite3.Row
    return conn

# setup(): open the DB, or warn and mark it unavailable. Idempotent and keyed on
# the app dict. Never raises — the server must boot without this file, and
# every response already carries `available` to say so.
def setup(app):
    if app.get("ultracheck_db") is not None:
        return
    db_path = app["cfg"]["ultracheck_db_path"]
    app["ultracheck_db_path"] = str(db_path)
    if not os.path.exists(db_path):
        print(f"warning: ultracheck dataset not found at {db_path}; "
              "partial-callsign matches will be empty (build it with "
              "data-parsers/ultracheck_update.py)")
        app["ultracheck_db"] = None
        return
    try:
        conn = _open(db_path)
        # Force a real read so a corrupt or truncated file fails here, at boot,
        # rather than on the first operator keystroke. `meta` is tiny and its
        # build stamp is a better freshness signal than the file's mtime, so
        # the check doubles as the log line an admin actually wants.
        built = conn.execute(
            "SELECT value FROM meta WHERE key = 'built_utc'").fetchone()
        count = conn.execute(
            "SELECT value FROM meta WHERE key = 'callsigns'").fetchone()
        app["ultracheck_db"] = conn
        print(f"ultracheck: {count['value'] if count else '?'} callsigns, "
              f"built {built['value'] if built else 'unknown'}")
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        print(f"warning: ultracheck dataset unavailable at {db_path} ({exc}); "
              "partial-callsign matches will be empty")
        app["ultracheck_db"] = None

# close(): release the handle. Idempotent. Matters on Windows, where an open
# handle keeps a test's scratch dir from being removed.
def close(app):
    conn = app.get("ultracheck_db")
    if conn is not None:
        conn.close()
        app["ultracheck_db"] = None