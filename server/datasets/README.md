# Zone boundary data

This directory contains vendored, static zone boundary data used by `server/zones.py`
It is used to derive amateur radio CQ and ITU zone numbers from a latitude/longitude coordinate.

## Files

| File              | Source zone numbering                       |
| ----------------- | ------------------------------------------- |
| `cqzones.geojson` | CQ Zones (integer `cq_zone_number`, 1–40)   |
| `ituzones.geojson`| ITU Zones (integer `itu_zone_number`, 1–90) |

Both files are GeoJSON `FeatureCollection`s of `Polygon` features. Coordinates
follow the GeoJSON axis order: `[longitude, latitude]`.

## Source

Both files were copied verbatim from:

- Repository: <https://github.com/hb9hil/hamradio-zones-geojson>
- Branch: `main`
- Files: `cqzones.geojson`, `ituzones.geojson`

## License

The data is distributed under the MIT License by the upstream author
(HB9HIL). The full MIT notice:

```
MIT License

Copyright (c) 2023 HB9HIL

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Vendoring date

This data was vendored into HAML on the date below. To refresh, re-download
from the upstream repository, verify license has not changed, and replace the
files in this directory.

- Vendored: 2026-05-13

## `fcc_amateur.sqlite`

The local FCC ULS operator dataset that `server/fcc.py` reads on every
callsign lookup. ~826k active US amateur licenses, one row per callsign.

- **Source**: FCC Universal Licensing System weekly data dump
  (`l_amat.zip` from <https://www.fcc.gov/ulrs>). The raw pipe-delimited
  extract is converted into this sqlite by an out-of-repo importer script
- **Schema** (table `operators`, with a unique index on `callsign`):
  - `callsign` TEXT PRIMARY KEY
  - `applicant_type` TEXT  — `Individual` / `Amateur Club` / `Military Recreation` / `Government Entity`
  - `first_name` TEXT, `middle_initial` TEXT, `last_name` TEXT, `name_suffix` TEXT
  - `entity_name` TEXT  — populated for non-individual applicants
  - `operator_class` TEXT, `previous_operator_class` TEXT  — single-letter codes (`A`/`E`/`G`/`N`/`P`/`T`)
  - `previous_callsign` TEXT, `trustee_callsign` TEXT, `trustee_name` TEXT, `attention_line` TEXT
  - `street_address` TEXT, `po_box` TEXT, `city` TEXT, `state` TEXT, `zip_code` TEXT
  - `frn` TEXT
  - `grant_date` TEXT, `expired_date` TEXT  — ISO `YYYY-MM-DD`
  - `gridsquare` TEXT  — 4-char Maidenhead field grid
  - `coordinates` TEXT  — `"lat,lon"` pre-geocoded by the importer
  - `country` TEXT     — country name (e.g. `"United States"`)
  - `continent` TEXT   — 2-letter continent code (e.g. `"NA"`)
  - `dxcc` INTEGER     — ARRL DXCC entity code (e.g. `291` for US)
- **Server config**: path overridable via `fcc_db_path` in the server
  config JSON. Default is `datasets/fcc_amateur.sqlite` (resolved
  relative to the server dir). A missing file is non-fatal: the server
  prints a warning at boot and lookups return 502.