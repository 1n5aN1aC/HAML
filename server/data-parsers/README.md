# data-parsers

Five importers that build `lookup_data.sqlite`, the shared lookup database for
callsigns, parks and geography. Each importer owns exactly one area of the
database and nothing else, so they can be run in any order, alone or all at
once, and one failing never costs another its data.

| Importer | Table(s) | Where the data comes from |
| --- | --- | --- |
| `importer_fcc.py` | `fcc_operators` | FCC weekly amateur dump `l_amat.zip`, geocoded with the US Census batch geocoder and Census county/ZCTA boundaries |
| `importer_ca.py` | `ca_operators` | ISED amateur callsign dump `amateur_delim.zip`, geocoded via geo.ca, with StatCan FSA and census-division boundaries |
| `importer_boundaries.py` | `counties` (+ parts/R\*Tree) | US Census county shapefiles + StatCan census divisions |
| `importer_pota.py` | `pota_parks` | `https://pota.app/all_parks_ext.csv` (regenerated daily) |
| `importer_zones.py` | `cq_zones`, `itu_zones`, `dxcc_entities` (+ parts/R\*Tree) | CQ/ITU zone and DXCC entity polygon repos, tracked by ETag on their default branch |

Downloads are kept between runs, so a rerun is normally cheap. Boundary and
reference sources are probed newest-first at run time rather than pinned, with a
fallback to the newest complete copy already on disk when a source is
unreachable.

Details of how any one importer works are in the docstring at the top of that
file — start there, not here.

## Setup

From this folder, once:

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
.venv\Scripts\python -m pip install -r requirements.txt
```

Use `.venv\Scripts\python -m pip`, not `pip.exe` — venv console shims hardcode
the interpreter path and break if the folder is moved. On macOS/Linux the
interpreter is `./.venv/bin/python`.

You do not have to "activate" the venv; calling its interpreter directly is
enough. If you prefer to activate it:

```bash
.venv\Scripts\Activate.ps1
```

(PowerShell; `.venv\Scripts\activate.bat` for cmd, `source .venv/bin/activate`
on macOS/Linux. `deactivate` to leave.)

## Running

```bash
.venv\Scripts\Activate.ps1
.venv\Scripts\python run_importers.py
```

That opens the TUI menu: `1` runs every importer and then compacts the database,
`2`–`6` run one, `q` quits. Ctrl-C returns you to the menu; the previously
published table is left intact, so a stopped run is safe to restart.

## Runtime estimates

As shown in the menu:

| Importer | Update | First/fresh run |
| --- | --- | --- |
| FCC amateur licenses | ~2 minutes | ~7 hours |
| Canada amateur licenses | ~2 minutes | ~6 hours, needs multiple runs |
| Boundaries (counties) | ~1 minute | |
| POTA parks | ~30 seconds | |
| Zones (CQ/ITU/DXCC) | ~30 seconds | |

The long first runs are geocoding; results are cached, which is why later runs
are minutes.

## Directories

    downloads/   files fetched from upstream, kept between runs
    caches/      work databases and the persistent geocode caches
    logs/        each run's log and reports (e.g. unmatched addresses)

Importers build into a work database under `caches/` and copy the finished table
into `lookup_data.sqlite` in one transaction at the end, so the published data is
never a half-finished run.