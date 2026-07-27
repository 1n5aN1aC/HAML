# Lookup datasets

This directory holds the datasets the server reads at runtime: the
`lookup_data.sqlite` bundle (gitignored, built out of repo) and the vendored
`Prefix.lst` prefix database.

## `lookup_data.sqlite`

One sqlite file holding every offline lookup dataset. `server/lookup_db.py`
opens it read-only once at boot and the adapters share that connection,
each querying its own table:

| Table                     | Reader                             |
| ------------------------- | ---------------------------------- |
| `fcc_operators`           | `server/lookup_fcc.py`             |
| `ca_operators`            | `server/lookup_ca.py`              |
| `cq_zones` / `itu_zones`  | `server/lookup_location_calc.py`   |
| `dxcc_entities`           | `server/lookup_location_calc.py`   |
| `counties`                | `server/lookup_location_calc.py`   |

- **Server config**: path overridable via `lookup_db_path` in the server
  config JSON. Default is `datasets/lookup_data.sqlite` (resolved
  relative to the server dir). A missing file is non-fatal: the server prints a
  warning at boot and lookups fall through to other sources. A 502 is returned
  only when nothing below resolves either.
- **Staleness warning**: because the upstream dumps refresh on their own
  cadence and the schema carries no build timestamp, `lookup_db.setup()` uses
  the file's mtime as a proxy for its build date and prints a boot-time
  warning when it is older than `lookup_db_max_age_days`.

### `fcc_operators`

The local FCC ULS operator dataset that `server/lookup_fcc.py` reads on every
callsign lookup. ~826k active US amateur licenses, one row per callsign.

- **Source**: FCC Universal Licensing System weekly data dump
  (`l_amat.zip` from <https://www.fcc.gov/ulrs>). The raw pipe-delimited
  extract is converted into this sqlite by an out-of-repo importer script
- **Schema** (with a unique index on `callsign`):
  - `callsign` TEXT PRIMARY KEY
  - `applicant_type` TEXT  — `Individual` / `Amateur Club` / `Military Recreation` / `Government Entity`
  - `first_name` TEXT, `middle_initial` TEXT, `last_name` TEXT, `name_suffix` TEXT
  - `entity_name` TEXT  — populated for non-individual applicants
  - `operator_class` TEXT, `previous_operator_class` TEXT  — single-letter codes (`A`/`E`/`G`/`N`/`P`/`T`)
  - `previous_callsign` TEXT, `trustee_callsign` TEXT, `trustee_name` TEXT, `attention_line` TEXT
  - `street_address` TEXT, `po_box` TEXT, `city` TEXT, `state` TEXT, `zip_code` TEXT
  - `county` TEXT
  - `arrl_section` TEXT — ARRL Section abbreviation (e.g. `"OR"`, `"ENY"`, `"MDC"`).
    Served to clients as the `section` field.
  - `frn` TEXT
  - `grant_date` TEXT, `expired_date` TEXT   — ISO `YYYY-MM-DD`. Note the column
    is `expired_date`; the canonical lookup record calls the field `expiry_date`.
  - `gridsquare` TEXT  — 4-char Maidenhead field grid
  - `coordinates` TEXT  — `"lat,lon"` pre-geocoded by the importer
  - `dxcc_entity` TEXT — DXCC entity name (e.g. `"United States"`, `"Alaska"`, `"Northern Mariana Islands"`). Served to clients as the `country` field.
  - `continent` TEXT   — 2-letter continent code (e.g. `"NA"`)
  - `dxcc_id` INTEGER  — ARRL DXCC entity code (e.g. `291` for US). Served to clients as the `dxcc` field.
### `ca_operators`

The local ISED operator dataset that `server/lookup_ca.py` reads. ~92k Canadian licenses

- **Source**: ISED's published amateur radio operator list. The raw extract is
  converted into this table by an out-of-repo importer script that emits the
  **same column layout as `fcc_operators`**, so the two
  adapters share all of their row -> canonical mapping.
- **Substantive differences from the FCC table** (all handled in `lookup_ca.py`):
  - ISED does **not** publish license dates, `frn`, `attention_line`, `po_box`,
    `middle_initial`, `name_suffix`, or any previous-callsign / previous-class /
    trustee-callsign history — those columns are always NULL and come back as
    clean `None` in the record.
  - `operator_class` holds a **set of qualification letters** (`A` Basic, `B`
    5 WPM, `C` 12 WPM, `D` Advanced, `E` Basic with Honours), e.g. `"ACD"`, not
    a single class letter. The adapter collapses the set to the single
    highest-privilege word (`advanced` > `basic with honours` > `basic`); a row
    holding only CW endorsements yields no class word (clean `None`).
  - `state` is a **Canadian province/territory code** (`ON`, `BC`, `QC`, …).
    The canonical-record state coercer (`lookup_record._CA_PROVINCE_CODES`)
    accepts these alongside US states; the client entry field already did.
  - `applicant_type` is only `Individual` or `Amateur Club`.
  - `arrl_section` holds a **RAC** section (`BC`, `ONS`, `GTA`, …) rather than an
    ARRL one; same column, same `section` field on the wire, different vocabulary.
- **Not cached**: like the FCC source, `CACHED = False` — the query is
  microseconds and a stale cache row would only outrank the DB.

### `cq_zones` / `itu_zones`

The CQ (1–40) and ITU (1–90) zone polygons `server/lookup_location_calc.py`
tests a coordinate against. Each zone is one row in the feature table
(`id`, `zone`, `name`, `label_lat`, `label_lon`, `area_deg2`); its geometry
lives in `{table}_parts` as one WKB polygon per row (`part_id`, `feature_id`,
`geom`), with an R\*Tree `{table}_bbox` (`id`, `minx`, `maxx`, `miny`, `maxy`,
`+feature_id`) over the parts. 44 CQ parts, 103 ITU parts.

- **Reading them**: the R\*Tree is a *prefilter* — it matches parts whose
  bounding box contains the point, and the point-in-polygon test decides.
  Join parts to bbox on `part_id = id`, so each row carries the one part
  whose box matched, and order by `z.id` so overlaps resolve deterministically.
- **Axis order**: the R\*Tree and the WKB blobs are `(x, y)` = `(lon, lat)`;
  `label_lat`/`label_lon` are the opposite order, and unused here.
- **Not a partition**: CQ covers ~96% of the globe, ITU ~94.5%, and a few
  pairs overlap. A point outside every polygon resolves to `None`, which is
  correct and reaches the client as a null zone.
- **Clipped at ±180**: every polygon stays within the antimeridian, so
  longitudes are plain values in that range; `lookup_location_calc` reads a
  longitude of +180 as -180, the side of that meridian the polygons cover.
- **Own connection**: unlike the operator tables, these are read through a
  second read-only handle opened lazily by `lookup_location_calc` itself,
  because its entry points take a bare coordinate and never see the app dict.

### `dxcc_entities`

The DXCC entity polygons `server/lookup_location_calc.py` turns a coordinate
into a country name and ARRL entity code. Same three-table shape as the zone
tables: `dxcc_entities` (`id`, `prefix`, `name`, `entity_code`, `area_deg2`),
`dxcc_entities_parts` (WKB, one polygon per row), `dxcc_entities_bbox`
(R\*Tree). 341 rows covering 340 entities — Conway Reef is two rows sharing
prefix `3D2/c` and code 489, so neither column is unique.

- **`id` ascends with polygon area, smallest first**, and ordering a
  point-in-polygon query by it is what resolves an enclave to the enclave:
  Vatican, San Marino and SMOM inside Italy, ITU HQ inside Switzerland, UN HQ
  inside the USA, Lesotho inside South Africa. Ordering by `entity_code`
  instead answers with the host country.
- **Land only**, ~33.8% of the globe, so a maritime coordinate resolves to
  nothing and reaches the client as a null country.
- **Coastline resolution** is a 1:110m generalisation, ~16,600 vertices
  worldwide, and neighbouring borders are generalised independently, so a
  point within a few km of a land border answers on the geometry's border.
  Vanuatu omits Efate, Tanna, Erromango and the Banks/Torres groups;
  Palestine is the West Bank only.
- **Entity vocabulary is DXCC's own**: `K` is `United States of America`,
  which matches `lookup_callparser` output rather than the `United States`
  that `fcc_operators.dxcc_entity` carries.
- **Prefixes are exact**: `3D2` is Fiji (176), while Conway Reef is `3D2/c`
  and Rotuma `3D2/r`.

### `counties`

The file's only administrative geography, and the source of `county`,
`state` and `section` in `server/lookup_location_calc.py`. 3,528 rows
(`id`, `county`, `state`, `country`, `arrl_section`) covering US counties and
Canadian census divisions, with geometry in `counties_parts` (180,961 WKB
polygons, 296 MB) under the `counties_bbox` R\*Tree.

- **All three fields come from one row**, so one query answers county, state
  and section together. `arrl_section` is populated on every row and holds
  the same value the operator tables carry, since one importer derives both.
- **`country` here is `US` or `CA`**, a two-letter code rather than a DXCC
  entity name, which is why `lookup_location_calc` takes `country` from
  `dxcc_entities` instead.
- **Order by `(country = 'US') DESC, id`** so a point on the 49th parallel
  resolves to the US side every time.
- **Geometry size drives the cost**: parts average 1.6 KB, but 30 run past a
  megabyte and mainland Baffin Island is a single 15.67 MB polygon, 979k
  points holding 110 MB once parsed into Python tuples. `lookup_location_calc`
  parses per lookup and keeps nothing, so a coordinate in the Arctic census
  divisions costs ~140 ms against ~1 ms elsewhere, and the server's memory
  does not grow with the area it has looked up.
- **The `county` vocabulary is whatever the authoritative source calls its
  county-equivalent**: Connecticut has nine planning regions (`Capitol`, not
  `Hartford`), Louisiana parishes, Alaska boroughs and census areas, and
  Canadian rows are census divisions named like `Division No. 18`.

## `Prefix.lst`

The VE3NEA CallParser prefix database, read by `server/callparser.py` (see
`server/lookup_callparser.py`). Committed to the repo; path overridable via
`prefix_lst_path`.
