# FCC Amateur Radio License Database — unified refresh pipeline

One command downloads the FCC's weekly amateur license dump, builds a clean
SQLite database, geocodes every licensee's address, stamps each row with a
4-character Maidenhead gridsquare, and assigns each row its county, DXCC
entity, continent, and ARRL section:

```
..\.venv\Scripts\python update_fcc_db.py
```

Output: **`fcc_amateur.sqlite`** — a single `operators` table, one row per
**active** license, with name, address, license class/dates, `coordinates`
(lat,lon), `gridsquare`, `geocode_match`, `county`, `dxcc_entity`/`dxcc_id`,
`continent`, and `arrl_section`.

* First-ever run (empty cache): **~1.5–2.5 hours**, almost all of it waiting on
  the free US Census geocoding service.
* Weekly/bi-weekly refresh (warm cache): **~10–20 minutes** — only new or
  changed addresses are geocoded; everything else is served from the local
  cache.

The run is **non-destructive until it succeeds**: the database and zip you
already have are never deleted up front. Each is replaced by an atomic rename
only once its replacement is complete and verified, so a failed download, a
failed build, or a `Ctrl-C` leaves you exactly as you were — and the existing
database stays queryable throughout, including during the long geocode.

Disk: ~1.8 GB peak during a run — the old database and zip coexist with their
replacements until the renames (~430 MB of that is the safety margin).

---

## Setup

The pipeline runs from a virtualenv, **not** the system Python. That venv lives
one level up, in `data-parsers/`, and is shared with the Canadian pipeline.
One-time, from `data-parsers/`:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Then always invoke the script through that interpreter — from this folder:

```bash
..\.venv\Scripts\python update_fcc_db.py       # Windows
../.venv/bin/python update_fcc_db.py           # macOS/Linux
```

or equivalently `.venv\Scripts\python FCC\update_fcc_db.py` from
`data-parsers/`. Every path the script uses is anchored on the script's own
location, so the working directory makes no difference.

Use `python -m pip`, not `pip` / `pip.exe`. The `.exe` shims inside a venv
hardcode an absolute path to the interpreter that created them, so if the
project folder is ever copied or renamed they quietly keep installing into the
old location.

The shared `../requirements.txt` lists four packages; this pipeline uses three
of them (`pyproj` is Canada-only):

| Package | Needed by |
|---|---|
| `requests` | everything — download and geocode |
| `shapely`, `pyshp` | Phase 6 (county) only — point-in-polygon against the Census boundary file |

Only `requests` is truly mandatory. `--no-county` runs without the other two,
at the cost of `county` being `NULL` everywhere and `arrl_section` being `NULL`
in the eight split states (CA/FL/MA/NJ/NY/PA/TX/WA), which resolve their
section from county. If they are missing and you did **not** pass that flag,
the script stops immediately rather than downloading 194 MB and geocoding for
an hour before finding out.

Everything else is stdlib. `.venv/` is deliberately excluded from version
control: a venv points at one specific base Python install and carries
compiled extensions built for that exact interpreter and platform, so it is
not portable to another machine. Recreating it from `../requirements.txt`
takes seconds.

---

## The ten phases

### Phase 1 — Cleanup

Deletes only what a **failed** run may have stranded: the half-built
`fcc_amateur.sqlite.new` (and its `-journal`), a partial `l_amat.zip.part`,
and any legacy `*.original.bak`.

**The live `fcc_amateur.sqlite` and `l_amat.zip` are deliberately not
deleted.** Each is replaced by an atomic rename only once its successor is
complete — the database in Phase 10, the zip in Phase 2 — so a run that dies
anywhere before then leaves the previous good copy exactly where it was. The
database you already have stays queryable for the entire run, including
throughout the ~1-hour geocode.

**The address-lookup cache (`geocode_cache\`) is never touched** — it is the
one thing worth keeping between runs. `update_run.log` is not deleted here;
it is truncated when the run opens it.

This phase is unconditional. It once had a `--skip-cleanup` escape hatch, from
when it deleted the database and zip outright and a failed run therefore cost
you both; now that it removes only wreckage, opting out would just mean
starting on top of the last crash's leftovers.

### Phase 2 — Download

Streams a fresh `l_amat.zip` (~175 MB) from
`https://data.fcc.gov/download/pub/uls/complete/l_amat.zip` with up to 6
retries. The download lands in `l_amat.zip.part` and is proved complete
(byte count matches `Content-Length`) and openable, with the FCC's `counts`
manifest present, **before** it is renamed over the previous copy — so a
failed or truncated fetch can never destroy a good one. The FCC refreshes
this file weekly (Sunday mornings, US time).

If all 6 attempts fail but the existing `l_amat.zip` is intact, the run
**continues on it** rather than abandoning everything downstream: a banner
records that the result will be only as current as that file, and every later
phase still runs to completion and still verifies. The run aborts only when
there is no usable zip at all. This replaced the old `--zip PATH` flag, whose
main purpose was exactly this fallback, done by hand.

### Phase 3 — Build

Parses the zip straight from memory (no extraction to disk) into
`fcc_amateur.sqlite`:

* The zip holds pipe-delimited, latin-1 files: `EN.dat` (entity/address),
  `HD.dat` (license status/dates), `AM.dat` (operator class etc.), plus
  detail files (`HS`, `CO`, `LA`, `SC`, `SF`) and a `counts` manifest.
* `HD.dat` is scanned first for **active** licenses (`license_status = 'A'`);
  everything else (cancelled/expired/terminated) is parsed for verification
  but not stored. Active-only means `callsign` is unique (UNIQUE index).
* `EN` drives the table; `HD` and `AM` merge in by the shared key
  `unique_system_identifier`. FCC codes are kept verbatim alongside decoded
  columns (`operator_class_desc`, `applicant_type`, `radio_service`); dates
  are normalized to ISO `YYYY-MM-DD`; empty fields become NULL.
* Data quirks handled: multi-line comment records in `CO.dat` are stitched;
  free-text fields containing `|` are re-joined; trailing whitespace stripped;
  mixed-case state codes (`Fl`, `az`) are normalized to uppercase USPS form
  (foreign/blank states left untouched).
* **Verification**: the raw line count of every file is checked against the
  FCC's own `counts` manifest, and the row count against the active-license
  count. Any mismatch aborts the pipeline (exit ≠ 0) before geocoding.

**Cleanup rules applied during the build:**

| Cleanup |
|---|
| Inactive licenses dropped (parsed, verified, not stored) |
| Columns 100% empty in the amateur dump (phone/fax/email — FCC-redacted — and ~30 non-amateur regulatory fields) never created |
| Constant-for-active columns (`entity_type`, `license_status`, `cancellation_date`) and ULS bookkeeping fields (`licensee_id`, `uls_file_number`, `sgin`, `effective_date`, `last_action_date`) not stored |
| Per-application detail files (HS/CO/LA/SC/SF) parsed for verification only, not stored |
| The five `certifier_*` columns (near-duplicates of the licensee's own name) excluded |

These columns are never built in the first place, rather than created and then
dropped — an identical final schema with no wasted I/O.

### Phase 4 — Geocode (online query, cached)

Adds street-level coordinates using the **US Census Bureau batch geocoder**
(free, no API key):

1. Extract every distinct normalized `(street, city, state, zip5)` from
   `operators` — ~736k unique addresses out of ~827k rows. Rows with no
   street address (PO-Box-only licensees) are not street-geocodable and wait
   for Phase 5.
2. Diff against the **persistent content-addressed cache**
   (`geocode_cache\geocode_cache.sqlite`, keyed by the normalized address
   itself, not by position — inserts/deletes/reorders in the source can never
   misalign it). Only addresses that are *new*, *changed*, or a *stale cached
   miss* (older than `--miss-retry-days`, default 30 — the Census benchmark
   improves over time) are queued.
3. Queued addresses POST in batches of 9,000 — fixed, not a flag. That is
   just under the service's 10,000 per-file cap: sitting exactly at the limit
   leaves no headroom for any disagreement about what counts as a row, and a
   smaller file is a shorter server-side job with less timeout exposure
   to `https://geocoding.geo.census.gov/geocoder/locations/addressbatch`
   (multipart form, `addressFile` + `benchmark=Public_AR_Current`), 3
   concurrent workers, up to 6 retries per batch with backoff. Every parsed
   batch — matches **and** misses — is committed to the cache immediately, so
   an interrupted run resumes where it left off; just rerun the same command.
   The Census service returns coordinates **longitude-first**; they are
   swapped to conventional `lat,lon` for storage.
4. Every `operators` row whose normalized address matched gets
   `coordinates`, `gridsquare`, and `geocode_match` (`Exact` or `Non_Exact`).

### Phase 5 — ZIP fallback (offline lookup)

Rows still without coordinates (PO-Box-only, unparseable or unmatched
addresses) are filled from the **Census ZCTA Gazetteer** ZIP-code
interior-point centroids (~1 MB, cached in `geocode_cache\` as
`2025_Gaz_zcta_national.zip`; see [Reference-file freshness](#reference-file-freshness)):

* **`Zip_Centroid`** — the row's own 5-digit ZIP has a ZCTA: use its centroid.
  Accurate to a few km — usually still the correct 4-char gridsquare.
* **`Zip_Approx`** — the ZIP has no ZCTA (PO-Box-only and "unique" ZIPs, which
  no census block uses as its delivery ZIP). These are almost always
  numerically adjacent to their town's street ZIP, so the centroid of the
  numerically nearest ZCTA sharing the same 3-digit prefix is used.
* **NULL** — the whole 3-digit prefix has no ZCTA (APO/FPO military mail,
  all-PO-box prefixes, foreign/invalid ZIPs). Left empty rather than risk a
  badly wrong location.

Disable with `--no-zip-fallback`. Coverage on the 2026-07-20 dump:
614,661 Exact + 107,652 Non_Exact + 80,209 Zip_Centroid + 14,284 Zip_Approx =
816,806 of 817,202 rows, **99.95%** located.

### Phase 6 — County assignment (offline lookup)

Every row with coordinates gets its **county (or equivalent)** by
point-in-polygon lookup against the Census Bureau **cartographic boundary
county file** (1:500,000 scale, ~11 MB, cached in `geocode_cache\` as
`cb_2025_us_county_500k.zip`; 3,235 polygons covering all states, DC, and
territories). Entirely offline and fast (~10 s for the full table):

* The lookup is **confined to counties of the row's own `state`**: the
  polygons are partitioned into one STRtree per state, and a point is only
  ever tested against its own. Without this, a geocode landing a few hundred
  metres across a state line is credited to the neighbouring state's county —
  measured at 173 rows, e.g. a Kansas City, KS address reported as Missouri's
  "Platte", or an Oregon border address as California's "Modoc". Those rows
  are also unmappable in Phase 9, since the section tables are keyed by
  (state, county).
* Distinct `(coordinates, state)` pairs are resolved once via a bulk `shapely`
  STRtree containment query, then written back to rows by primary key. Adding
  `state` to the key costs almost nothing — 658,430 pairs vs 658,362 distinct
  coordinates.
* Stored value is the **short name** — `NAME`, not `NAMELSAD` — so
  "Jefferson", not "Jefferson Parish"; "Anchorage", not "Anchorage
  Municipality". Louisiana parishes, Alaska boroughs, Puerto Rico municipios,
  DC, and independent cities all come through with the suffix-free name.
  Names are **not** unique nationally (there is a "Washington" county in 30
  states) — combine with `state` when aggregating.
* Points inside no polygon **of their own state** (street matches geocoded
  slightly offshore, coastal ZIP centroids) snap to the **nearest** county in
  that state, mirroring the Zip_Approx philosophy — but only within **0.5°**
  (~55 km). Measured over the full dataset, 127 of the 241 such points fall
  within that line, and nearly all within 0.25°: these are true coastal
  misses, and snapping them is right.
* Beyond 0.5°, the row is left **NULL** rather than snapped. A point that far
  from every county of its own state means the *coordinate* is wrong, not the
  polygon — almost always a mistyped ZIP that sent Phase 5 to the wrong end of
  the country. 114 points (115 rows) hit this, the nearest 56 km out and the
  worst 268° away: a Guam ZIP (`96903`) on a Weaverville, California address,
  and a Tacoma, Washington address on a licensee registered in California.

  These used to be snapped and merely logged as a `WARNING`. That was worse
  than useless: the result was a real county of the right state, so nothing
  downstream could tell it from a genuine point-in-polygon hit, and Phase 9
  turned it into an equally confident — and equally wrong — `arrl_section`.
  Refusing to guess costs 115 counties and 29 sections out of 817,202 rows
  (county coverage 99.950% → 99.937%) and removes every fabricated one. The
  ten worst are still logged, now as a report of what was dropped.
* `county` is NULL where `coordinates` is NULL, and for the handful of rows
  whose state has no US county-equivalents at all (`UM`, US Minor Outlying
  Islands — 3 rows). Those are left NULL rather than snapped to whichever
  mainland county happens to be closest.

Disable with `--no-county`.

### Phase 7 — DXCC entity (offline, state-derived)

Every row is tagged with its **ARRL DXCC entity** — how the amateur world
splits the US-and-affiliated area into "countries" for awards/logging:

* DXCC entity is determined by the station's **physical location, not its
  callsign**. US callsigns are portable — a `KH6` (Hawaii) prefix can be held
  from Ohio, and a plain mainland `W` call held from Hawaii; measured
  agreement between prefix and address is only ~83–93%. The licensee's
  **`state` code** is therefore the key, not the callsign.
* The 48 contiguous states + DC are the single entity **United States**
  (`dxcc_id` 291). Alaska (6), Hawaii (110), Puerto Rico (202), US Virgin
  Islands (285), Guam (103), Northern Mariana Islands (166), and American
  Samoa (9) are each **separate** DXCC entities.
* APO/FPO military codes (`AA`/`AE`/`AP`) become `Military (APO/FPO)` with a
  NULL `dxcc_id` — the station could physically be anywhere. Foreign, blank,
  or `UM` (US Minor Outlying Is.) states are left NULL rather than guessed.
* Matching is case-insensitive, so stray lowercase state codes (`Fl`, `az`)
  are handled without a separate cleanup pass.

Two columns: `dxcc_entity` (name) and `dxcc_id` (ARRL entity number).
Disable with `--no-dxcc`.

### Phase 8 — Continent (offline, DXCC-derived)

Fills the `continent` column from each row's `dxcc_id` via a small lookup
table: the contiguous US, Alaska, Puerto Rico, and the US Virgin Islands are
**`NA`** (North America); Hawaii, Guam, the Northern Marianas, and American
Samoa are **`OC`** (Oceania). Rows with no `dxcc_id` — APO/FPO military and
undeterminable states — keep a NULL continent.

Disable with `--no-continent`. `--no-dxcc` also skips this phase: without
Phase 7 there is no `dxcc_id` to derive from and every row would be NULL.

### Phase 9 — ARRL Section (offline, state + county derived)

Derives the **ARRL Section** abbreviation (`arrl_section`) — the contest and
field-organization subdivision — from each licensee's state code:

* Most states are a single section and resolve from `state` alone.
* Eight states are split into multiple sections and need the county from
  Phase 6 to disambiguate: **CA, FL, MA, NJ, NY, PA, TX, WA**. (Running with
  `--no-county` leaves these rows unresolved.)
* Maryland and DC merge into **MDC**; Hawaii and the Pacific territories merge
  into **PAC**.

The phase first validates the county→section tables against the data actually
present and logs any county name it cannot map. Since Phase 6 confines each
lookup to the licensee's own state, an unmappable name now means a genuine gap
in the tables rather than a cross-state geocode snap, and is worth
investigating. It then writes one `UPDATE`
per non-split state and per split-state county, ~660 statements; these ride
the `(state, county)` index, without which each would scan the whole table.

Disable with `--no-section`.

### Phase 10 — Finalize

* `VACUUM` to compact the file (the geocode updates leave free pages behind).
* Prints a coverage summary by `geocode_match` and the total runtime. The
  whole console output is also written to `update_run.log` (UTF-8).
* **Renames `fcc_amateur.sqlite.new` onto `fcc_amateur.sqlite`.** This is the
  one destructive moment in the run, and the last. `os.replace` is atomic on
  both Windows and POSIX, so the name never points at a partial database — it
  is the previous run's up to this instant and this run's immediately after.
  It creates the file when no previous database exists, and overwrites it when
  one does.

  If the rename is refused — on Windows, another process holding the old file
  open, typically a SQLite browser — the run does **not** discard its work: it
  reports that the finished database is complete at the `.new` path and exits
  normally. Close whatever holds the file and rename it by hand, or just
  rerun.

---

## Database schema

Single table **`operators`**, one row per active license, `callsign` UNIQUE:

| group | columns |
|---|---|
| key | `unique_system_identifier` (PK, FCC ULS id), `callsign` |
| identity | `entity_name`, `first_name`, `middle_initial`, `last_name`, `name_suffix`, `frn`, `applicant_type_code` + `applicant_type` (Individual / Amateur Club / Military Recreation / RACES / Government …) |
| address | `street_address`, `city`, `state`, `zip_code`, `po_box`, `attention_line` |
| license | `radio_service_code` + `radio_service` (HA Amateur / HV Vanity), `grant_date`, `expired_date` (ISO), `convicted` |
| amateur | `operator_class` + `operator_class_desc` (Technician / General / Amateur Extra / Advanced / Novice / Technician Plus), `group_code`, `region_code`, `previous_callsign`, `previous_operator_class`, `vanity_call_sign_change`, trustee callsign/indicator/name (clubs) |
| location | `coordinates` ("lat,lon" WGS-84, 6 decimals), `gridsquare` (4-char Maidenhead), `geocode_match` (`Exact` / `Non_Exact` / `Zip_Centroid` / `Zip_Approx` / NULL), `county` (short name, no "County"/"Parish" suffix) |
| DXCC | `dxcc_entity` (ARRL entity name — United States / Alaska / Hawaii / Puerto Rico / …), `dxcc_id` (ARRL entity number), `continent` (`NA` / `OC`) |
| ARRL | `arrl_section` (ARRL Section abbreviation — e.g. `MO`, `EPA`, `SDG`, `MDC`, `PAC`) |

The 4-character Maidenhead locator is computed from the coordinates: field
letters from 20°×10° cells, square digits from 2°×1° cells (e.g. `EN75`).

### Example queries

```sql
SELECT * FROM operators WHERE callsign = 'W1AW';

-- everyone in a gridsquare
SELECT callsign, entity_name, city, state
FROM operators WHERE gridsquare = 'EM48';

-- coverage by match quality
SELECT geocode_match, COUNT(*) FROM operators GROUP BY 1;

-- Extras per state
SELECT state, COUNT(*) FROM operators
WHERE operator_class = 'E' GROUP BY 1 ORDER BY 2 DESC;

-- hams per county in one state (county names repeat across states,
-- so always pair county with state)
SELECT county, COUNT(*) FROM operators
WHERE state = 'MO' GROUP BY 1 ORDER BY 2 DESC;

-- US-affiliated DXCC entities only (exclude the contiguous US)
SELECT dxcc_entity, dxcc_id, COUNT(*) FROM operators
WHERE dxcc_id IS NOT NULL AND dxcc_id <> 291
GROUP BY 1 ORDER BY 3 DESC;
```

Two indexes:

| index | purpose |
|---|---|
| `idx_operators_callsign` (UNIQUE) | single-callsign lookup, and the uniqueness guarantee that active-only licensing gives |
| `idx_operators_state_county` | `(state, county)` — makes the "hams per county in *state*" queries above a seek instead of a scan, and cuts Phase 9 from ~83 s to ~3 s |

Anything else full-scans in ~1–2 s, which is fine for a file this size.

---

## Routine use

```
python update_fcc_db.py                # weekly refresh, warm cache
```

### Fixed paths

Every file the pipeline owns lives beside the script under a fixed name:

| path | what it is |
|---|---|
| `fcc_amateur.sqlite` | the database — built as `.new`, renamed on success |
| `l_amat.zip` | the FCC dump — downloaded as `.part`, then renamed |
| `update_run.log` | this run's log (truncated at start) |
| `geocode_cache\` | persistent cache + Census reference files (`--cache-dir`) |

There is no flag to relocate the database (`--db`) or to supply your own zip
(`--zip`). Both existed to work around a Phase 1 that deleted them before the
download had succeeded — `--db` to write somewhere safe, `--zip` to protect
and reuse a copy you already had. Phase 1 now removes only the wreckage of a
failed run, and Phase 2 falls back to the existing zip on its own, so both
flags were left with nothing to protect against and only ways for a run's
artifacts to drift apart.

All flags:

| flag | default | purpose |
|---|---|---|
| `--cache-dir PATH` | `geocode_cache` | cache location |
| `--workers N` | 3 | concurrent Census uploads |
| `--miss-retry-days D` | 30 | retry cached misses older than D days (0 = always) |
| `--no-zip-fallback` | off | skip Phase 5 |
| `--no-county` | off | skip Phase 6 (county assignment) |
| `--no-dxcc` | off | skip Phase 7 (DXCC entity) — also skips Phase 8 |
| `--no-continent` | off | skip Phase 8 (continent NA/OC lookup) |
| `--no-section` | off | skip Phase 9 (ARRL section assignment) |
| `--no-ref-check` | off | never check the reference files for updates (offline) |

Exit status is 0 only if every verification passed and no batch failed
permanently. The pipeline is **safe to rerun after any failure** — the cache
preserves all completed geocoding work.

### Interrupting a run

Killing the run at any point leaves `fcc_amateur.sqlite` intact and valid: it
is either the previous run's database or, if the interrupt came after the
Phase 10 rename, this run's. It is never a partial file.

Rerunning picks up as follows:

* **Phase 1** clears the abandoned `.new` and any `.part`, sparing the live
  database and zip.
* **Phase 2 and 3** redo from scratch — the download does not resume from a
  partial `.part`, so an interrupt during the download costs the whole
  ~175 MB.
* **Phase 4 resumes**, which is the phase that matters: every completed batch
  is already committed to the cache, so only addresses that never got a result
  are re-queried. Interrupting after 40 of 74 batches costs you 34, not a
  fresh ~2-hour geocode. Leftovers in `_work\` are never resume state and are
  purged at the start of the phase.
* **Phases 5–10** are cheap and simply redo.

Do **not** start a second run while one is still alive — there is no lock
file, and the second run's Phase 1 and `_work\` purge will pull files out from
under the first.

## The cache (`geocode_cache\`)

* `geocode_cache.sqlite` — two tables. `geocode_cache` holds one row per
  normalized address ever queried: coordinates + match quality for hits, a
  timestamped tombstone for misses (~100 MB for the full dataset). This is why
  refreshes are fast, and it is the only thing Phase 1 preserves — deleting it
  forces a full ~2-hour re-geocode.
* `2025_Gaz_zcta_national.zip` — the ZCTA gazetteer (~1 MB), re-downloaded
  automatically if missing. The year in the name **is** its vintage.
* `cb_2025_us_county_500k.zip` — the Census county boundary file (~11 MB),
  re-downloaded automatically if missing.
* `_work\` — in-flight batch files; removed as batches complete. Leftovers
  after a crash are harmless — they are never re-read (the cache is the only
  resume state) and are purged at the start of the next geocode phase.
* Old addresses of licensees who moved linger unused in the cache — harmless,
  no pruning needed.

### Reference-file freshness

Both Census files are named after the vintage they came from
(`cb_2025_us_county_500k.zip`), and the vintage in use is **pinned in
`update_fcc_db.py`**:

```python
COUNTY_KEY: {
    "vintage": 2025,          # <- change this to adopt a newer release
```

The pin and the filename are the same fact, so there is nothing to keep in
sync and no metadata to go stale. Four cases, and that is the whole design:

| Situation | What happens |
|---|---|
| The pinned file is in the cache | Used as-is. No network. |
| It is not | Downloaded, verified as a readable zip, and the superseded vintage deleted. |
| The download fails, but an older vintage is in the cache | **That older file is used** and a banner says so. The run completes on slightly stale boundaries rather than not at all. |
| A newer vintage exists upstream | A banner tells you the year and the exact line to edit. Never adopted automatically. |

Adopting a new release is therefore a one-number edit and a rerun — which also
makes the change reviewable, unlike a silent auto-update. Neither file updates
itself: county `NAME` feeds both the `county` column and the Phase 9 ARRL
section lookup, so a renamed or resplit county would quietly change results,
and the gazetteer follows the same rule to avoid a special case.

The check costs a couple of `HEAD` requests and runs on **every** invocation
by design, so a pending upgrade keeps announcing itself until you take it. If
the Census is unreachable, the check logs a warning and the run continues on
the local copy; `--no-ref-check` skips it entirely for offline runs.

This used to be a `reference_files` table in the cache database tracking
`vintage`, `fetched_at`, `checked_at`, `source_url`, `etag`, `last_modified`,
`size`, and `sha256`, with conditional `HEAD` requests to detect same-vintage
reissues, per-file auto-update policies, and an `--update-refs` flag. That is
all gone — about 200 lines and a database table replaced by a number in a
dict and a filename that states its own vintage.

## Troubleshooting

* **Census HTTP 502/timeouts** — routine; the service is flaky under load.
  Batches retry 6 times with backoff. If a batch still fails permanently the
  run exits non-zero: just rerun, only the failed addresses are re-queried.
* **Build verification MISMATCH** — the zip was truncated or the FCC changed
  the format. Delete `l_amat.zip` and rerun; if it persists, compare the
  layouts in `update_fcc_db.py` against the FCC "ULS Public Access Database
  Definitions". The failed build stays under `fcc_amateur.sqlite.new` for
  inspection and is never promoted; your previous database is untouched.
* **"falling back to the local zip" banner** — the FCC was unreachable for all
  6 attempts and the run rebuilt from the `l_amat.zip` already on disk. The
  database is complete and fully verified, but only as current as that file.
  Rerun once the FCC is back.
* **"could not replace the previous one" banner** — the finished database is
  complete at `fcc_amateur.sqlite.new`, but something has the old file open
  (usually a SQLite browser). Close it, then rename by hand or rerun.
* **A leftover `fcc_amateur.sqlite.new`** — the residue of a failed or
  interrupted run. Harmless; the next run's Phase 1 removes it.
* **`counts` manifest quirk** — the manifest is a raw line count, so embedded
  newlines inside `CO.dat` comments legitimately make logical records fewer
  than raw lines; the verifier accounts for this.
* **Interrupted run** — rerun the same command. Your existing
  `fcc_amateur.sqlite` is still intact and valid; Phase 4 resumes from the
  cache and earlier phases are fast enough to just redo. See
  [Interrupting a run](#interrupting-a-run) for the full picture.
