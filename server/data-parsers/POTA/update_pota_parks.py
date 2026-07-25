#!/usr/bin/env python3
"""
update_pota_parks.py - refresh the POTA park list.

POTA publishes the whole park list as one CSV, which is exactly the format the
consumer reads, so this script only downloads and verifies - there is no build
or convert phase (unlike update_fcc_db.py / update_ca_db.py, which have to turn
their sources into sqlite).

Source: https://pota.app/all_parks_ext.csv  (regenerated daily)
Output: all_parks_ext.csv, next to this script.

Stdlib only - no virtualenv, no requirements.txt.

The download lands in a .part file and is only renamed over the previous copy
once the header and row count check out, so a truncated download or an error
page can never replace a good file. Same rule as the other downloaders: the
live artifact is replaced by an atomic rename or not at all.

Exit code 0 on success, 1 on failure (so a master runner can just check it).
"""
import csv
import os
import sys
import urllib.request
from pathlib import Path

URL = "https://pota.app/all_parks_ext.csv"
HERE = Path(__file__).resolve().parent
OUT = HERE / "all_parks_ext.csv"
PART = OUT.with_suffix(".csv.part")

# Columns the consumer needs. Extra columns are fine (everything downstream
# reads by header name); a MISSING one means the schema changed and the file
# must not be installed.
REQUIRED_COLUMNS = (
    "reference", "name", "active", "entityId",
    "locationDesc", "latitude", "longitude", "grid",
)

# Floor for a plausible file. There were ~93,700 parks in mid-2026 and the list
# only grows, so anything this far below it is a truncated download, not a real
# shrink. Raise it as the list grows if you want a tighter tripwire.
MIN_ROWS = 50_000

TIMEOUT = 120


def download(url, dest):
    """Stream `url` into `dest`. Returns bytes written."""
    request = urllib.request.Request(url, headers={"User-Agent": "haml-pota-downloader"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, open(dest, "wb") as fh:
        while chunk := response.read(1 << 16):
            fh.write(chunk)
    return dest.stat().st_size


def verify(path):
    """Check the header carries every required column and count data rows.

    Returns the row count. Raises ValueError on anything that would make the
    file unsafe to install.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError("file is empty")
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            # Truncate what we echo back: when the server hands us an HTML error
            # page instead of the CSV, its first "column" is the whole document.
            got = ", ".join(header)
            if len(got) > 120:
                got = got[:120] + "..."
            raise ValueError(f"header is missing {', '.join(missing)} (got: {got})")
        rows = sum(1 for _ in reader)
    if rows < MIN_ROWS:
        raise ValueError(f"only {rows:,} rows, expected at least {MIN_ROWS:,} - download looks truncated")
    return rows


def main():
    PART.unlink(missing_ok=True)  # a previous run may have died mid-download
    print(f"downloading {URL}")
    try:
        size = download(URL, PART)
        rows = verify(PART)
    except Exception as err:
        PART.unlink(missing_ok=True)
        existing = f"; keeping the previous {OUT.name}" if OUT.exists() else ""
        print(f"FAILED: {err}{existing}", file=sys.stderr)
        return 1
    os.replace(PART, OUT)
    print(f"wrote {OUT.name}: {rows:,} parks, {size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
