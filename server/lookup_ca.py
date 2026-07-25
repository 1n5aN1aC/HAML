"""ISED (Canada) adapter: turns one row of `operators` into the canonical record.

Structurally a twin of `lookup_fcc`: a pure adapter over a local sqlite,
opened read-only at setup time, handing back the {status, payload, error}
shape on a miss or an unavailable DB. It sits directly after `lookup_fcc`
in `lookup.SOURCES` — a US call resolves before this module is ever asked,
and a VE/VA/VO/VY call falls through the FCC miss into here.

The Canadian dataset is built to the FCC column layout on purpose (see
`server/datasets/README.md`), so most of the mapping is identical. The three
real differences are handled below: ISED publishes no license dates / FRN /
previous-call history (those columns are always NULL), the qualification
codes are a multi-letter set rather than a single class letter, and `state`
is a province code.

`CACHED = False`, same reasoning as the FCC source: the query is
microseconds, a cache row buys no latency, and a stale row would outrank the
DB itself. Cache writes are the dispatcher's job regardless — this module
never touches the cache.
"""
import os
import sqlite3
import time

import lookup_cache
import lookup_record

SOURCE = "ised"

# Offline and instant: nothing to gain from a cache row. See lookup.SOURCES.
CACHED = False


# --- applicant_type -> license_type (canonical enum, lowercased by coerce) --
# ISED only distinguishes individuals from clubs; the importer emits the same
# two strings the FCC layout uses, so the map is the FCC one minus the two
# US-only rows. Anything unrecognized passes through as the raw string and
# the client simply skips the name fill (its gate is `license_type === 'person'`).
_APPLICANT_TYPE_MAP = {
    "Individual": "person",
    "Amateur Club": "club",
}

# --- qualification letters -> a single license_class word ------------------
# Canada issues *qualifications*, not classes: a licensee holds any subset of
#   A Basic   B 5 WPM   C 12 WPM   D Advanced   E Basic with Honours
# and `operator_class` is the concatenation ("ACD", "DE", "E", ...). The
# canonical record's `license_class` is one lowercase word (Callook
# vocabulary for the FCC source), and the client only ever displays it, so we
# collapse the set to the highest privilege held rather than inventing a
# 16-value enum. Advanced outranks Basic with Honours outranks Basic; the CW
# qualifications (B/C) are endorsements on top of one of those and are never
# the whole story, so they never decide the word on their own.
_CLASS_BY_PRIORITY = (
    ("D", "advanced"),
    ("E", "basic with honours"),
    ("A", "basic"),
)

def _license_class(operator_class):
    """Highest-privilege qualification held, as one lowercase word.

    '' when the column is empty or holds only CW endorsements — coerce()
    turns that into a clean None rather than a dirty field, which is the
    right answer for "we can't summarize this in one word".

    Policy: a row whose only letters are B/C (CW endorsements, no A/D/E)
    is deliberately treated as "no privilege to report" (clean None), not
    as an unparseable/dirty value — the endorsements simply have no class
    word to stand in for on their own.
    """
    held = set((operator_class or "").strip().upper())
    for letter, word in _CLASS_BY_PRIORITY:
        if letter in held:
            return word
    return ""

# --- open the read-only DB connection ---------------------------------------
# `uri=True` + `mode=ro` is the official way to open a sqlite read-only via a file: URI.
def _open(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # Row factory so we can read by column name in _build_record(); ordinal
    # positions would be fragile against importer-side column reordering.
    conn.row_factory = sqlite3.Row
    return conn

# Staleness warning: when the DB is older than the configured threshold.
def _warn_if_stale(db_path, max_age_days):
    if not max_age_days:  # 0 disables the check
        return
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        return  # File-age unknowable; the open path already warns on a bad file.
    age_days = (time.time() - mtime) / 86400
    if age_days > max_age_days:
        print(
            f"warning: Canadian dataset at {db_path} is {age_days:.1f} days old "
            f"(threshold {max_age_days}); the ISED amateur list refreshes "
            "regularly, consider rebuilding it"
        )

# setup(): called from main.build_app via lookup.setup.
# Missing/unopenable -> warn, store None. We never raise; the server must
# boot so the admin endpoints still work.
def setup(app):
    db_path = app["cfg"]["ca_db_path"]
    try:
        conn = _open(db_path)
        # Force a real open + pragma so a corrupt file fails here, not on the first lookup.
        conn.execute("PRAGMA quick_check").fetchone()
        app["ca_db"] = conn
        app["ca_db_path"] = str(db_path)
        _warn_if_stale(db_path, app["cfg"].get("ca_db_max_age_days", 0))
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        # Not fatal: the chain falls through to the prefix DB.
        print(
            f"warning: Canadian dataset unavailable at {db_path} ({exc}); "
            "Falling back to other sources"
        )
        app["ca_db"] = None
        app["ca_db_path"] = str(db_path)

# close(): called from lookup.close() at shutdown. The read-only handle is
# process-lived otherwise; closing it lets a test's scratch dir be removed.
def close(app):
    conn = app.get("ca_db")
    if conn is not None:
        conn.close()
        app["ca_db"] = None

# --- row -> canonical mapping ----------------------------------------------
# coerce() turns empty strings into None and tracks dirty fields; we only
# build the shape.
#
# Columns that ISED does not publish are always NULL in this dataset and so
# are simply absent from the record we build: frn, grant_date, expired_date,
# attention_line, po_box, middle_initial, name_suffix, previous_callsign,
# previous_operator_class, trustee_callsign. They come back as clean Nones.
def _build_record(row):
    applicant_type = (row["applicant_type"] or "").strip()
    license_type = _APPLICANT_TYPE_MAP.get(applicant_type, applicant_type)

    license_class = _license_class(row["operator_class"])

    # Name: Individual -> "FIRST LAST", else the club/org entity_name.
    # ISED publishes no middle initial or suffix, so there is nothing to
    # compose beyond the two parts.
    if applicant_type == "Individual":
        parts = [
            (row["first_name"] or "").strip(),
            (row["last_name"] or "").strip(),
        ]
        name = " ".join(p for p in parts if p)
    else:
        name = row["entity_name"] or ""

    # Address: ISED does not split out a PO box, so whatever is on file —
    # including "PO BOX 126" — arrives in street_address already.
    address_line1 = (row["street_address"] or "").strip()

    # address_line2 is the CITY, ST POSTAL shape. Canadian postal codes carry
    # their own internal space ("B4E 2X8"); that is the published form and
    # the field is display-only, so it goes through as-is.
    city = (row["city"] or "").strip()
    state = (row["state"] or "").strip()
    zip_code = (row["zip_code"] or "").strip()
    if city and state and zip_code:
        address_line2 = f"{city}, {state} {zip_code}"
    elif city and state:
        address_line2 = f"{city}, {state}"
    else:
        address_line2 = ""

    # coordinates is a single text column "lat,lon" from the importer's
    # geocode step. Split here; let coerce() judge the floats.
    coords = (row["coordinates"] or "").strip()
    latitude = ""
    longitude = ""
    if coords:
        bits = coords.split(",", 1)
        if len(bits) == 2:
            latitude = bits[0].strip()
            longitude = bits[1].strip()

    return {
        "callsign": row["callsign"] or "",
        "name": name,
        "license_type": license_type,
        "license_class": license_class,
        "trustee_name": row["trustee_name"] or "",
        "address_line1": address_line1,
        "address_line2": address_line2,
        "state": state, # 2-letter province/territory code
        "county": row["county"] or "",
        "section": row["arrl_section"] or "", # RAC section abbreviation
        "country": row["dxcc_entity"] or "",
        "continent": row["continent"] or "",
        "latitude": latitude,
        "longitude": longitude,
        "gridsquare": row["gridsquare"] or "", # 6-char; coerce() truncates to the 4-char field grid
        "dxcc": row["dxcc_id"] if row["dxcc_id"] is not None else "",
    }

# lookup(): one indexed query; sync because the work is microseconds.
# Returns the {status, payload, error} shape the chain expects.
def lookup(app, callsign):
    conn = app.get("ca_db")
    if conn is None:
        return {
            "status": lookup_cache.STATUS_ERROR,
            "payload": {},
            "error": "lookup dataset unavailable",
        }

    try:
        row = conn.execute(
            "SELECT * FROM operators WHERE callsign = ?", (callsign,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # The DB file disappeared or got corrupted between setup() and now.
        return {
            "status": lookup_cache.STATUS_ERROR,
            "payload": {},
            "error": f"lookup dataset error: {exc}",
        }

    if row is None:
        return {
            "status": lookup_cache.STATUS_NOT_FOUND,
            "payload": {},
            "error": "callsign not found",
        }

    raw = _build_record(row)
    record, bad_fields = lookup_record.coerce(raw)

    # Stamp source + fetched_at here, since the cache layer is bypassed.
    record["source"] = SOURCE
    record["fetched_at"] = lookup_record.now_iso()

    if bad_fields:
        print(
            f"warning: ised record for {callsign} has dirty fields: "
            f"{', '.join(bad_fields)}"
        )
    return {
        "status": lookup_cache.STATUS_OK,
        "payload": record,
        "error": "",
    }