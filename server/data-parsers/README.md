# Amateur radio callsign database builders

Two independent pipelines that download a national amateur-radio licence dump
and turn it into a clean, geocoded SQLite database.

| | Source | Script | Output |
|---|---|---|---|
| **[FCC/](FCC/)** | US — FCC ULS weekly dump | `update_fcc_db.py` | `fcc_amateur.sqlite` |
| **[Canada/](Canada/)** | Canada — ISED amateur data | `update_ca_db.py` | `ca_amateur.sqlite` |

Both produce a single `operators` table with the **same columns in the same
order**, so the two can be queried uniformly or `UNION`ed together. Fields one
country does not publish are left `NULL`.

Each row carries name, address, licence class and dates, plus `coordinates`,
`gridsquare` (Maidenhead), `geocode_match`, `county`, `dxcc_entity` / `dxcc_id`,
`continent`, and `arrl_section`.

## Quick start

The two pipelines are entirely separate and share no code, so you can use one
and ignore the other — but they **share one venv**, here in `data-parsers/`,
built from the single `requirements.txt` beside it (the union of what both
need). One-time setup:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

Then run either script through that interpreter:

```bash
.venv/Scripts/python FCC/update_fcc_db.py
.venv/Scripts/python Canada/update_ca_db.py
```

Each script anchors every path it uses on its own location, so it does not
matter which directory you invoke it from — output lands beside the script
either way.

On macOS/Linux the interpreter is `.venv/bin/python` instead of
`.venv/Scripts/python`.

Note the `python -m pip` form rather than `pip` / `pip.exe`. The `.exe` shims
in a venv hardcode an absolute path to the interpreter that created them, so
if you ever copy or rename the project folder they quietly keep installing
into the *old* location. `python -m pip` always targets the interpreter you
invoked it with.

That is the whole setup. There is nothing to configure: every path each
pipeline uses is fixed, beside its own script. Run it again to refresh.

## What to expect on a first run

| | FCC | Canada |
|---|---|---|
| Download | ~194 MB | ~2 MB |
| Cold run | ~1.5–2.5 h | ~7 h |
| Warm re-run | ~10–20 min | minutes |
| Peak disk | ~1.8 GB | ~800 MB |

Almost all of that time is the free public geocoding service, not local work.
Reruns are much faster because every completed address lookup is kept in
`geocode_cache/` and reused.

**Both scripts are safe to interrupt.** `Ctrl-C` flushes finished lookups to
the cache and exits; rerunning the same command picks up where it left off. The
database you already have is never deleted up front — it is replaced by an
atomic rename only after its replacement is complete and verified, so a failed
download, a failed build, or a `Ctrl-C` leaves you exactly as you were, and the
existing database stays queryable for the entire run.

**The Canadian geocoder is flaky by design of the upstream service** — geo.ca
returns HTTP 500 on roughly half of all requests, at random. That is expected,
not a bug. Just run the script again; each pass fills in more from the cache.
See [Canada/NOTES.md](Canada/NOTES.md).

## Requirements

Python 3.9+ and `requests`. The point-in-polygon phases additionally need
`shapely` + `pyshp` (FCC) and `shapely` + `pyshp` + `pyproj` (Canada); the
shared `requirements.txt` lists all four.

If those are missing, the script says so **immediately** and tells you either
what to install or which `--no-*` flag skips the phase — it does not download
200 MB and geocode for an hour first.

Run `python update_fcc_db.py --help` (or `update_ca_db.py --help`) for the full
flag list.

## Documentation

* [FCC/README.md](FCC/README.md) — phase-by-phase walkthrough of the US pipeline
* [Canada/NOTES.md](Canada/NOTES.md) — the Canadian pipeline, and a long write-up
  of the geo.ca quirks the validation logic exists to defend against

## Data sources and licensing

The data comes from the US and Canadian governments and is redistributed under
their terms, not this project's:

* FCC ULS — <https://data.fcc.gov/download/pub/uls/complete/l_amat.zip> (US
  public domain)
* ISED amateur data — <https://ised-isde.canada.ca/site/amateur-radio-operator-certificate-services/en/downloads>
  (Open Government Licence – Canada)
* US Census geocoder, ZCTA gazetteer, county boundaries (US public domain)
* Statistics Canada boundary files (Statistics Canada Open Licence)

Both databases contain **names and home addresses of licensed amateurs**,
published by the respective regulators. Redistributing a built database is not
the same as redistributing the scripts — check the source terms before you do.

> **Note:** these are read-only bulk-download clients. Neither script needs an
> API key, and neither sends anything anywhere except the address strings the
> geocoders need.
