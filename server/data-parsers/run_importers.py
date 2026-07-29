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

One step in the menu is not an importer of that database: ultracheck builds its
own ultracheck.sqlite from six public callsign sources (ultracheck_update.py).
It is here because it is the same weekly refresh, run from the same menu.

Importers are run IN THIS PROCESS: the menu imports the module and calls its
run(), which is the same thing as a flagless command-line run of that script.
Each importer is therefore expected to expose run() and to signal failure by
raising SystemExit with a message - which is what sys.exit() in its phases
already does. Anything else it raises is caught here too, so a bug in one
importer returns you to the menu rather than ending the session.

The databases are built here, not where the server reads them: an import takes
hours and the server should stay on the last good copy for all of them. Both
finished files are copied into ../datasets/ - automatically at the end of
`run all`, or on their own from the menu.

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
import shutil
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
ULTRACHECK_DB_PATH = os.path.join(HERE, "ultracheck.sqlite")

# Where the server reads the databases from.
# Building in place here and copies when a run is completes
SERVER_DB_PATH = os.path.join(HERE, os.pardir, "datasets", "lookup_data.sqlite")
SERVER_ULTRACHECK_DB_PATH = os.path.join(HERE, os.pardir, "datasets",
                                         "ultracheck.sqlite")


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

# Menu numbering: 1 runs everything, importers start at 2, and the two steps
# that are not importers take the keys after the last one.
FIRST_IMPORTER_KEY = 2
ULTRACHECK_KEY = FIRST_IMPORTER_KEY + len(IMPORTERS)
DEPLOY_KEY = ULTRACHECK_KEY + 1

# These only need a label: neither is an importer and neither owns a table in
# lookup_data.sqlite, but both appear in the same menu and the same summary.
DEPLOY_LABEL = "Copy databases to the server"
ULTRACHECK_LABEL = "Ultracheck call database"


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


def copy_db(src, dest):
    """Copy one finished database to where the server reads it.

    Written to a temporary file alongside the destination and moved into place,
    so a copy interrupted half way through cannot leave the server a truncated
    database. Returns True/False; like an importer it reports rather than
    raises, so `run all` still reaches its summary.
    """
    dest = os.path.abspath(dest)
    print(f"\n  Copying to {dest}")
    if not os.path.exists(src):
        print(f"  Nothing to copy: {os.path.basename(src)} does not exist yet.")
        return False

    tmp = dest + ".new"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        size = os.path.getsize(src)
        print(f"  {size / 1e9:,.2f} GB ..." if size >= 1e9
              else f"  {size / 1e6:,.0f} MB ...")
        shutil.copyfile(src, tmp)
        # Replacing an open file fails on Windows: SQLite opens without
        # FILE_SHARE_DELETE, so a running server holds the old database down.
        os.replace(tmp, dest)
    except OSError as e:
        print(f"  FAILED: {e}")
        print("  If the server is running, stop it and copy again.")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    print("  Done.")
    return True


def deploy():
    """Copy both finished databases to where the server reads them.

    Both are attempted even if the first fails - they are independent files and
    a stale one is no reason to leave the other stale too. Returns 'ok' only if
    both arrived.
    """
    print(f"\n{'=' * 70}\n  {DEPLOY_LABEL}\n{'=' * 70}")
    results = [copy_db(DB_PATH, SERVER_DB_PATH),
               copy_db(ULTRACHECK_DB_PATH, SERVER_ULTRACHECK_DB_PATH)]
    return "ok" if all(results) else "failed"


def run_ultracheck():
    """Build ultracheck.sqlite, the partial-callsign search database.

    A flagless run of ultracheck_update.py: fetch all six sources and merge them
    in. The build is accumulative and never deletes, so this is safe to repeat;
    only its own --rebuild is destructive, and that is not offered here.

    It reports its own per-source failures and returns non-zero when any of them
    failed, so like an importer this returns 'ok'/'failed' rather than raising.
    """
    print(f"\n{'=' * 70}\n  {ULTRACHECK_LABEL}  ->  ultracheck.sqlite\n{'=' * 70}")
    t0 = time.time()
    try:
        mod = importlib.import_module("ultracheck_update")
        code = mod.main([])
    except KeyboardInterrupt:
        raise
    except ImportError as e:
        print(f"\n  {ULTRACHECK_LABEL} FAILED: {e}")
        print("  A required package is missing. Install the requirements:")
        print("    python -m pip install -r requirements.txt")
        return "failed"
    except Exception:
        print(f"\n  {ULTRACHECK_LABEL} FAILED with an unexpected error:")
        traceback.print_exc()
        return "failed"
    print(f"\n  {ULTRACHECK_LABEL}: finished in {(time.time() - t0) / 60:,.1f} min")
    return "ok" if code == 0 else "failed"


def run_all():
    """Run every importer, continuing past failures."""
    results = []
    interrupted = False
    for imp in IMPORTERS:
        try:
            results.append((imp.label, run_importer(imp)))
        except KeyboardInterrupt:
            print("\n\n  Ctrl-C: stopping. Importers already finished keep "
                  "their tables; the rest were not started.")
            results.append((imp.label, "interrupted"))
            interrupted = True
            break

    vacuum()

    # Outside the interrupted check: it builds its own database and shares
    # nothing with the importers above, so an abandoned lookup_data run is no
    # reason to skip it.
    try:
        results.append((ULTRACHECK_LABEL, run_ultracheck()))
    except KeyboardInterrupt:
        print("\n\n  Ctrl-C: stopping. ultracheck.sqlite keeps whatever the "
              "last finished build left in it.")
        # Does not stop the copy below: that publishes lookup_data.sqlite,
        # which this step never touches.
        results.append((ULTRACHECK_LABEL, "interrupted"))

    # Last, and not offered as a question: a finished `run all` is exactly when
    # the server should get the new database. An interrupted one is not
    # finished, so the server keeps the copy it has.
    if interrupted:
        print(f"\n  Not copying to the server: the run did not finish. "
              f"Use option {DEPLOY_KEY} when it does.")
    else:
        results.append((DEPLOY_LABEL, deploy()))
    summary(results)


def summary(results):
    print(f"\n{'=' * 70}\n  Summary\n{'=' * 70}")
    for label, outcome in results:
        mark = {"ok": "OK", "failed": "FAILED",
                "interrupted": "interrupted"}[outcome]
        print(f"  {mark:<12} {label}")
    failed = sum(1 for _, o in results if o == "failed")
    if failed:
        print(f"\n  {failed} step(s) failed. See logs/ for the details of "
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
    print(f"   {ULTRACHECK_KEY}  {ULTRACHECK_LABEL}")
    print("          (~1 minute)")
    print(f"   {DEPLOY_KEY}  {DEPLOY_LABEL}")
    print("          (done automatically at the end of option 1)")
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
        if choice == str(ULTRACHECK_KEY):
            try:
                outcome = run_ultracheck()
            except KeyboardInterrupt:
                print("\n\n  Ctrl-C: stopped. ultracheck.sqlite keeps whatever "
                      "the last finished build left in it.")
                continue
            summary([(ULTRACHECK_LABEL, outcome)])
            continue
        if choice == str(DEPLOY_KEY):
            summary([(DEPLOY_LABEL, deploy())])
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
                summary([(imp.label, outcome)])
                continue
        print(f"\n  '{choice}' is not one of the options.")


def quit_now(code):
    """Leave without waiting for a Ctrl-C'd importer's worker threads.

    A stopped importer leaves up to WORKERS threads parked in a socket read
    that can take half an hour to time out, and ThreadPoolExecutor's atexit
    hook JOINS them - so a normal exit would sit there long after the menu said
    goodbye. The importers commit their own work and close their own databases
    before the interrupt ever reaches us, so there is nothing left for the
    interpreter to shut down cleanly.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    quit_now(main())
