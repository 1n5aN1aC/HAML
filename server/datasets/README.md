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