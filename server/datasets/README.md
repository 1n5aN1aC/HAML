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

## `Prefix.lst`

The VE3NEA CallParser prefix database, read by `server/callparser.py` (see
`server/lookup_callparser.py`). Committed to the repo; path overridable via
`prefix_lst_path`.
