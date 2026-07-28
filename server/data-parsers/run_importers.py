#!/usr/bin/env python3
r"""
run_importers.py - the menu that runs every data importer into lookup_data.sqlite.

Each importer owns exactly one table in the shared database and nothing else, so
they can be run in any order, individually or all at once, and one failing never
costs another its data:

    lookup_data.sqlite
        fcc_operators     FCC amateur licenses      importer_fcc.py
        ca_operators      ISED amateur callsigns    importer_ca.py
        counties          US+CA boundaries (+R*Tree) importer_boundaries.py
        pota_parks        Parks on the Air list     importer_pota.py
        cq_zones, itu_zones, dxcc_entities          importer_zones.py
                          CQ/ITU zone and DXCC entity polygons (+R*Tree)

Importers are run IN THIS PROCESS: the menu imports the module and calls its
run(), which is the same thing as a flagless command-line run of that script.
Each importer is therefore expected to expose run() and to signal failure by
raising SystemExit with a message - which is what sys.exit() in its phases
already does. Anything else it raises is caught here too, so a bug in one
importer returns you to the menu rather than ending the session.

Every importer shares three directories under this one:

    downloads/   files fetched from upstream, kept between runs
    caches/      work databases and reusable lookup caches
    logs/        each run's log and reports

Usage
-----
    .venv\Scripts\python run_importers.py

One-time setup, from this folder:
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
"""

import importlib
import os
import sqlite3
import sys
import time
import traceback
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))

DOWNLOADS_DIR = os.path.join(HERE, "downloads")
CACHES_DIR = os.path.join(HERE, "caches")
LOGS_DIR = os.path.join(HERE, "logs")
DB_PATH = os.path.join(HERE, "lookup_data.sqlite")


# --------------------------------------------------------------------------- #
# The importers
#
# `module` is imported lazily, at the moment the importer is actually run: a
# missing third-party package (shapely, pyshp, requests) would otherwise stop
# the menu from opening at all, and stop you running the importers that do not
# need it.
# --------------------------------------------------------------------------- #

class Importer(NamedTuple):
    label: str
    module: str
    table: str
    note: str = ""


IMPORTERS = [
    Importer("FCC amateur licenses", "importer_fcc", "fcc_operators",
             "    (~2 minutes to update) Fresh:  ~7 hours"),
    Importer("Canada amateur licenses", "importer_ca", "ca_operators",
             "    (~2 minutes to update) Fresh:  ~6 hours, needs multiple runs"),
    Importer("Boundaries (counties)", "importer_boundaries", "counties",
             "    (~1 minute)"),
    Importer("POTA parks", "importer_pota", "pota_parks",
             "    (~30 seconds)"),
    Importer("Zones (CQ/ITU/DXCC)", "importer_zones", "cq_zones + itu_zones + dxcc_entities",
             "    (~30 seconds)"),
]

# Menu numbering: 1 runs everything, importers start at 2.
FIRST_IMPORTER_KEY = 2


def run_importer(imp):
    """Run one importer. Returns 'ok' or 'failed'.

    Never raises for an importer's own failure - the point of the summary is
    that `run all` reaches the end and tells you what happened to each one.
    KeyboardInterrupt is the exception: that is the user talking to the menu,
    not an importer failing, so it propagates.
    """
    print(f"\n{'=' * 70}\n  {imp.label}  ->  {imp.table}\n{'=' * 70}")
    t0 = time.time()
    try:
        mod = importlib.import_module(imp.module)
        mod.run()
    except KeyboardInterrupt:
        raise
    except SystemExit as e:
        # How the importers' phases report a handled failure (sys.exit("ERROR:
        # ...")). A bare/zero exit is a success that just chose to stop early.
        if e.code in (0, None):
            print(f"\n  {imp.label}: finished in {(time.time() - t0) / 60:,.1f} min")
            return "ok"
        print(f"\n  {imp.label} FAILED: {e.code}")
        return "failed"
    except ImportError as e:
        print(f"\n  {imp.label} FAILED: {e}")
        print("  A required package is missing. Install the requirements:")
        print("    python -m pip install -r requirements.txt")
        return "failed"
    except Exception:
        # Unhandled: show the traceback, because this one is a bug rather than
        # an expected condition like an unreachable server.
        print(f"\n  {imp.label} FAILED with an unexpected error:")
        traceback.print_exc()
        return "failed"
    print(f"\n  {imp.label}: finished in {(time.time() - t0) / 60:,.1f} min")
    return "ok"


def vacuum():
    """Compact the database: publishing a table leaves the old copy's pages free."""
    print(f"\n{'=' * 70}\n  Compacting {os.path.basename(DB_PATH)}\n{'=' * 70}")
    # isolation_level=None: VACUUM cannot run inside the driver's implicit transaction.
    con = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        con.execute("VACUUM")
    finally:
        con.close()


def run_all():
    """Run every importer, continuing past failures."""
    results = []
    for imp in IMPORTERS:
        try:
            results.append((imp, run_importer(imp)))
        except KeyboardInterrupt:
            print("\n\n  Ctrl-C: stopping. Importers already finished keep "
                  "their tables; the rest were not started.")
            results.append((imp, "interrupted"))
            break

    vacuum()
    summary(results)


def summary(results):
    print(f"\n{'=' * 70}\n  Summary\n{'=' * 70}")
    for imp, outcome in results:
        mark = {"ok": "OK", "failed": "FAILED",
                "interrupted": "interrupted"}[outcome]
        print(f"  {mark:<12} {imp.label}")
    failed = sum(1 for _, o in results if o == "failed")
    if failed:
        print(f"\n  {failed} importer(s) failed. See logs/ for the details of "
              f"each run.")


def menu():
    print(f"\n{'=' * 70}")
    print("  Data importers -> lookup_data.sqlite")
    print(f"{'=' * 70}")
    print(f"  Database: {DB_PATH}"
          f"{'' if os.path.exists(DB_PATH) else '  (not created yet)'}")
    print()
    print("   1  Run ALL importers")
    for i, imp in enumerate(IMPORTERS):
        key = FIRST_IMPORTER_KEY + i
        print(f"   {key}  {imp.label}")
        if imp.note:
            print(f"      {imp.note}")
    print("   q  Quit")
    print()


def main():
    # Importers are imported from this directory, whatever the working
    # directory of the shell that started the menu.
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    for d in (DOWNLOADS_DIR, CACHES_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)

    while True:
        menu()
        try:
            choice = input("  Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "1":
            run_all()
            continue
        if choice.isdigit():
            idx = int(choice) - FIRST_IMPORTER_KEY
            if 0 <= idx < len(IMPORTERS):
                imp = IMPORTERS[idx]
                try:
                    outcome = run_importer(imp)
                except KeyboardInterrupt:
                    print("\n\n  Ctrl-C: stopped. The previously published "
                          "table is untouched; rerun to resume.")
                    continue
                summary([(imp, outcome)])
                continue
        print(f"\n  '{choice}' is not one of the options.")


if __name__ == "__main__":
    sys.exit(main())
