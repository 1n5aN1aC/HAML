"""FCC ULS adapter: turns one row of `operators` into the canonical record.

A pure adapter — no HTTP, no async, no I/O beyond the local sqlite read.
File is opened read-only at setup time so a misconfigured path never blocks boot.
On miss or DB unavailable, hand back a {status, payload, error} shape.

The chain seam is left visible in `lookup._run_lookup`: future online
providers (QRZ, HamQTH) append there, own their own HTTP sessions and
gates, and write results into the cache. This module writes nothing.
"""
import sqlite3

import lookup_cache
import lookup_record
import zones

SOURCE = "fcc"


# --- applicant_type -> license_type (canonical enum, lowercased by coerce) --
# Mirrors the four states the client `lookup-fill.js` gates on.
# Anything we don't recognize passes through as the raw applicant_type string;
# coerce() will lowercase it and the client will simply skip the name fill (the gate is `license_type === 'person'`).
_APPLICANT_TYPE_MAP = {
    "Individual": "person",
    "Amateur Club": "club",
    "Military Recreation": "military",
    "Government Entity": "races",
}

# --- license_class code -> Callook-vocabulary word -------------------------
# Callook uses lowercase spelled-out class names; FCC ULS uses single-letter
# codes. The client's lookup-fill.js only consumes `license_class` to
# display it, but the canonical record is the same shape regardless of
# provider, so we map to the same lowercase vocabulary.
_LICENSE_CLASS_MAP = {
    "A": "advanced",
    "E": "extra",
    "G": "general",
    "N": "novice",
    "P": "technician plus",
    "T": "technician",
}

# --- open the read-only DB connection ---------------------------------------
# `uri=True` + `mode=ro` is the official way to open a sqlite read-only via a file: URI.
def _open(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # Row factory so we can read by column name in _build_record().
    # Default tuples would force us to track ordinal positions, which is
    # fragile against importer-side column reordering.
    conn.row_factory = sqlite3.Row
    return conn

# setup(): called from main.build_app.
# Missing/unopenable -> warn, store None.
# We never raise; the server must boot so the admin endpoints still work.
def setup(app):
    db_path = app["cfg"]["fcc_db_path"]
    try:
        conn = _open(db_path)
        # Force a real open + pragma so a corrupt file fails here, not on the first lookup.
        conn.execute("PRAGMA quick_check").fetchone()
        app["fcc_db"] = conn
        app["fcc_db_path"] = str(db_path)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        print(
            f"warning: FCC dataset unavailable at {db_path} ({exc}); "
            "callsign lookup will return 502 until the file is present"
        )
        app["fcc_db"] = None
        app["fcc_db_path"] = str(db_path)

# --- row -> canonical mapping ----------------------------------------------
# coerce() will turns empty strings into None and dirty fields are tracked there.
# We only build the shape; the coercer is the single source of truth on what counts as a parse failure.
def _build_record(row):
    # sqlite3.Row supports both indexing and keys(); the schema is fixed.
    applicant_type = row["applicant_type"] or ""
    license_type = _APPLICANT_TYPE_MAP.get(applicant_type, applicant_type)

    # FCC ULS names these operator_class / previous_operator_class; the
    # canonical record calls them license_class / previous_license_class.
    license_class = _LICENSE_CLASS_MAP.get(
        (row["operator_class"] or "").strip().upper(), "")

    previous_license_class = _LICENSE_CLASS_MAP.get(
        (row["previous_operator_class"] or "").strip().upper(), "")

    # Name: Individual -> composed "FIRST M LAST [SUFFIX]", else entity_name.
    if (row["applicant_type"] or "").strip() == "Individual":
        parts = [
            (row["first_name"] or "").strip(),
            (row["middle_initial"] or "").strip(),
            (row["last_name"] or "").strip(),
        ]
        # middle_initial is "" for most people; dropping the empty join element
        # keeps the name as "FIRST LAST" rather than "FIRST  LAST".
        name = " ".join(p for p in parts if p)
        suffix = (row["name_suffix"] or "").strip()
        if suffix:
            name = f"{name} {suffix}"
    else:
        name = row["entity_name"] or ""

    # Address: street preferred, else synthesize "PO BOX {po_box}".
    # If only a PO box is on file, street_address is NULL/empty and we fall back.
    street = (row["street_address"] or "").strip()
    po_box = (row["po_box"] or "").strip()
    if street:
        address_line1 = street
    elif po_box:
        address_line1 = f"PO BOX {po_box}"
    else:
        address_line1 = ""

    # address_line2 is the CITY, ST ZIP shape.
    city = (row["city"] or "").strip()
    state = (row["state"] or "").strip()
    zip_code = (row["zip_code"] or "").strip()
    if city and state and zip_code:
        address_line2 = f"{city}, {state} {zip_code}"
    elif city and state:
        address_line2 = f"{city}, {state}"
    else:
        address_line2 = ""

    # coordinates is a single text column "lat,lon" produced by the importer's pre-geocode step.
    # Split here; let coerce() decide whether the resulting floats are valid.
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
        "previous_callsign": row["previous_callsign"] or "",
        "previous_license_class": previous_license_class,
        "trustee_callsign": row["trustee_callsign"] or "",
        "trustee_name": row["trustee_name"] or "",
        "address_line1": address_line1,
        "address_line2": address_line2,
        "address_attn": row["attention_line"] or "",
        "state": state, # 2-digit USPS code
        "county": row["county"] or "",
        "country": row["country"] or "",
        "continent": row["continent"] or "",
        "latitude": latitude,
        "longitude": longitude,
        "gridsquare": row["gridsquare"] or "",
        "dxcc": row["dxcc"] if row["dxcc"] is not None else "",
        "frn": row["frn"] or "",
        "grant_date": row["grant_date"] or "",
        "expiry_date": row["expired_date"] or "",
    }

# lookup(): one indexed query; sync because the work is microseconds.
# Returns the {status, payload, error} shape the chain seam expects.
def lookup(app, callsign):
    conn = app.get("fcc_db")
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

    # Derive CQ + ITU zones from the coordinates when we have them.
    # Only-fill-if-null:
    if record.get("latitude") is not None and record.get("longitude") is not None:
        derived = zones.derive(record["latitude"], record["longitude"])
        if record.get("itu_zone") is None:
            record["itu_zone"] = derived["itu_zone"]
        if record.get("cq_zone") is None:
            record["cq_zone"] = derived["cq_zone"]

    # Stamp source + fetched_at here, since the cache layer is bypassed for FCC
    record["source"] = SOURCE
    record["fetched_at"] = lookup_record.now_iso()

    dirty = bool(bad_fields)
    if dirty:
        print(
            f"warning: fcc record for {callsign} has dirty fields: "
            f"{', '.join(bad_fields)}"
        )
    return {
        "status": lookup_cache.STATUS_OK,
        "payload": record,
        "error": "",
    }