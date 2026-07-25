# Canada Amateur Radio Callsign Database — Notes

## ⚠️ The geocoding lookup is flaky — expect to run the script more than once
The NRCan / geo.ca geolocation service returns **HTTP 500 on roughly half of all
requests**, at random and unrelated to the address being queried (see
[Geocoder](#geocoder--nrcan--geoca-geolocation-service) for the full write-up).
The pipeline is built to cope — it retries each lookup, only caches real answers,
and **resumes from the cache** — but a single run can still finish with a batch of
addresses that never got a `200`. That is normal, not a bug.

**So: just run `update_ca_db.py` again.** Every rerun reuses the cache and only
re-queries the addresses that never answered, so each pass fills in more and costs
only minutes (a warm-cache rerun is far faster than the ~7 h first run). Keep
rerunning the same command until the located percentage stops climbing. If a run
warns that **>50 % of a phase's lookups never answered**, geo.ca was degraded at
the time — wait a bit and rerun. Ctrl-C is safe at any point; it flushes progress
to the cache and the next run picks up where it left off.

## Environment / how to run
The script uses four non-stdlib packages, so run it from a virtualenv, **not**
the system Python. That venv lives one level up, in `data-parsers/`, and is
shared with the FCC pipeline.

```bash
# one-time setup, from data-parsers/
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
#  ./.venv/bin/python -m pip install -r requirements.txt       # macOS/Linux

# always invoke the pipeline through the venv interpreter (from this folder):
../.venv/Scripts/python.exe update_ca_db.py         # Windows
#  ../.venv/bin/python update_ca_db.py               # macOS/Linux
```

The script anchors every path it uses on its own location, so the working
directory makes no difference — `.venv/Scripts/python.exe Canada/update_ca_db.py`
from `data-parsers/` does the same thing.

The shared `../requirements.txt` lists them:

| Package | Needed by |
|---|---|
| `requests` | everything (download + geocode) |
| `shapely`, `pyshp`, `pyproj` | Phases 6 (postal/FSA), 7 (province), 8 (county) — the point-in-polygon phases |

The geo packages are optional in the sense that `--no-postal --no-province
--no-county` runs without them, leaving `county`, the Ontario sections, and the
FSA cross-check `NULL`. If they are missing and you did **not** pass those
flags, the script stops immediately and says so — it does not download and
geocode for hours first.

Everything else is Python stdlib. There is a single script — `update_ca_db.py`
both downloads the ISED data and builds the database (the old standalone
`downloader.py` has been folded into it, so there is no separate download step
to run).

## Source
Government of Canada / ISED (Innovation, Science and Economic Development Canada),
Amateur Radio Operator Certificate Services.

- Downloads page: https://ised-isde.canada.ca/site/amateur-radio-operator-certificate-services/en/downloads

## Files available (ISED data server: https://apc-cap.ic.gc.ca/datafiles/)
| File | Contents |
|------|----------|
| `amateur_delim.zip`     | **Main callsign list, `;`-delimited TXT** (what we use) |
| `amateur.zip`           | Same data, fixed-width standard TXT format |
| `special_callsign.zip`  | Special event call signs (past/present/future) |
| `amateur_exmr.zip`      | Accredited examiner list |
| `amat_basic_quest.zip`  | Basic exam question bank |
| `amat_adv_quest.zip`    | Advanced exam question bank |

## Download details (verified 2026-07-20)
- URL: `https://apc-cap.ic.gc.ca/datafiles/amateur_delim.zip`
- HTTP 200, `application/zip`, ~2.3 MB compressed / ~6.6 MB uncompressed.
- No authentication, no headers, no cookies required — a plain GET works.
- HTTP redirects to HTTPS; the HTTPS URL works directly.
- The zip is regenerated regularly (mtime tracks recent dates), so re-download to refresh.

## Zip contents
- `amateur_delim.txt`        — the data (has a header row)
- `readme_amat_delim.txt`    — English field layout
- `lisezmoi_amat_delim.txt`  — French field layout

## Record format (`;`-separated, one record per line)
The file's own header row uses these field names:
```
callsign;first_name;surname;address_line;city;prov_cd;postal_code;
qual_a;qual_b;qual_c;qual_d;qual_e;
club_name;club_name_2;club_address;club_city;club_prov_cd;club_postal_code
```
Per `readme_amat_delim.txt`, the qualifications map to:
- `qual_a` = BASIC (A)
- `qual_b` = 5 WPM (B)
- `qual_c` = 12 WPM (C)
- `qual_d` = ADVANCED (D)
- `qual_e` = Basic with Honours (E)

A qualification field holds the letter (e.g. `A`) if held, else empty.
Club fields are populated only for club/sponsored callsigns.

~91,930 records + 1 header row.

## Encoding
The data file is **UTF-8** (verified: valid UTF-8, accented names like
`Sauvé` and en-dashes intact). This differs from the FCC dump, which is
latin-1. The build reads with `encoding="utf-8"`.

## Step 1 status: DONE (merged into update_ca_db.py)
Downloading the zip, extracting it, and validating the result (header + a
non-empty record count) is Phase 1–2 of `update_ca_db.py`. The former
standalone `downloader.py` was merged in and removed — there is one script.

### Reuse flag (skip re-downloading the ISED data)
- `--skip-download` — do NOT fetch from ISED; reuse the `amateur_delim.zip`
  already in this folder, or, if only the extracted `amateur_delim.txt` is left,
  validate and use that directly. (`--zip PATH` still works to point at a
  specific zip elsewhere.) The reused files are treated as input, so Phase 1
  cleanup and the end-of-run tidy leave them in place.

Auxiliary data files (the StatCan census-division boundary zip in the cache dir,
used for county / section) are already reused automatically whenever present —
they are only downloaded once, on the first run that needs them.

## Step 2 status: DONE
`update_ca_db.py` builds `ca_amateur.sqlite` — one `operators` table whose
column layout is **byte-identical** to `FCC/fcc_amateur.sqlite` for columns
1-38 (verified: same names/types), plus one Canada-only column `arrl_section`
(see Step 4). At Step 2 the address-derived phases were stubbed; they are now
implemented (geocode + county in Step 3, section in Step 4).

### Field mapping (Canada -> FCC schema)
| FCC column | Canada source | Notes |
|---|---|---|
| `unique_system_identifier` | (synthetic) | sequential row id; Canada has no numeric id, `callsign` is the real unique key |
| `callsign` | callsign | |
| `entity_name` | display name / club_name(+2) | "First Surname" for individuals; club name for clubs |
| `first_name` / `last_name` | first_name / surname | NULL for club rows (person moves to `trustee_name`) |
| `street_address`/`city`/`state`/`zip_code` | address_line/city/prov_cd/postal_code | for clubs, the club's own address is used |
| `applicant_type_code`+`applicant_type` | derived | `I`/Individual or `B`/Amateur Club |
| `operator_class` | qual_a..qual_e | held letters concatenated, e.g. `ACD` |
| `operator_class_desc` | qual_a..qual_e | decoded, e.g. `Basic; 12 WPM; Advanced` |
| `trustee_name` | first_name+surname (club rows) | the club's accredited sponsor |
| `dxcc_entity`/`dxcc_id` | (all Canada) | `Canada` / `1` |
| `continent` | (all Canada) | `NA` |

Columns Canada does not publish (middle_initial, name_suffix, po_box,
attention_line, frn, radio_service*, dates, convicted, group/region codes,
trustee_callsign/indicator, vanity/previous fields, and the geocode columns)
are always created but left NULL — keeping the layout identical to FCC.

### Cleanups (parallel to the FCC side)
- All licenses kept (the ISED file lists only current assignments — there is no
  status field to filter on, unlike FCC's active-only rule).
- Fields stripped of whitespace; empty -> NULL.
- Province codes upper-cased to canonical form.
- Postal codes upper-cased and reformatted to `A1A 1A1` when they match the
  canonical pattern; malformed codes (O/0 typos, wrong length — ~a handful) are
  left as cleaned text rather than guessed.
- Verification: rows stored == source data lines, and callsigns are unique
  (the FCC's `counts`-manifest check has no equivalent, so this stands in).

Data shape (2026-07-22): 91,926 rows = 88,703 individuals + 3,223 clubs; every
row carries at least one qualification.

### Measured effect of the pick_city name/kind fix (2026-07-22 run)
The city cache was cleared (`invalidate_city_cache.py`) and all 1,124 town
lookups re-run under the new rules, so the same keys were resolved both ways:

| | keys |
|---|---|
| unchanged | 553 |
| **moved** — had pointed at the wrong place | **229** (median 255 km, max 1,430 km) |
| **hit → NULL** — had been pure noise | **287** |
| NULL → hit (bonus recovery) | 7 |
| still no match | 48 |

**516 of 1,069 (48%) previously-'successful' town centroids were wrong.** The
rate is higher than the 39% measured on a random sample because Phase 5 only
ever sees the residue Phase 4 could not place, which skews toward small or
obscure places geo.ca does not carry.

Cost: 1,539 rows (1.67%) that used to get a *wrong* coordinate are now honest
NULLs, so located fell 78.16% → **76.49%**. Independent verification —
point-in-polygon of every coordinate against the StatCan boundary file's PRUID,
which shares no logic with the name filter — found **0 province mismatches
across all 3,440 `City_Centroid` rows** (4 of 66,871 street rows mismatch, an
unrelated pre-existing street-picker artifact).

## Step 3 status: DONE (geocoding + gridsquare + county)
`update_ca_db.py` Phases 4-5 resolve addresses to coordinates and stamp a
Maidenhead gridsquare; Phase 6 fills `county`. CQ/ITU zones were dropped from
scope (per the user).

### County (Phase 6) — census divisions
`county` is filled by point-in-polygon of each geocoded row's coordinates
against Canada's **census divisions** (the county-equivalent), from the same
StatCan boundary file Phase 9 uses (`load_cd_polygons`, all 293 divisions).
Stores the short division name (`cd_short_name`, bilingual/whitespace cleaned)
— the direct analog of the FCC pipeline's short county name. Distinct
coordinates are resolved once; points inside no division snap to the nearest.
Names are counties (`Halifax`, `Frontenac`), regional municipalities
(`Greater Vancouver`), districts, and StatCan's numbered `Division No. N` in
the west (AB/SK/MB/NL). `county` is NULL exactly where `coordinates` is NULL.
Needs `shapely`/`pyshp`/`pyproj` (skip with `--no-county`).

### Geocoder — NRCan / geo.ca geolocation service
- Endpoint: `https://www.geolocator.api.geo.ca/geolocation/en/locate?q=<query>`
  (the older `geogratis.gc.ca/services/geolocation/...` URL 302-redirects here).
- Free, no key, **one query at a time** (no batch API, unlike the US Census).
- Returns a JSON list of candidates sorted by relevance, each with a `type`
  (`...model.Street`, `...Address`, `...Intersection`, `...Geoname`) and a
  **longitude-first** `geometry.coordinates`. We take the first Street/Address
  result for street lookups, the first Geoname for city lookups.
- **Quirk that cost real time:** the service returns **HTTP 500 intermittently**
  (~50% of the time) with an identical body `{"message":"Internal server
  error"}` — and this is the ONLY error it emits. Probing showed the 500 is
  pure random flakiness, unrelated to the address: a perfect address 500s as
  often as pure garbage. It is almost always transient, so `geoloc()` retries
  up to 4 times with a SHORT backoff, then gives up (the address is recorded as
  a miss and the city fallback catches it). The earlier ALL-CAPS hypothesis was
  wrong; we query in the source's original casing anyway.
- **No "not found" signal — must validate the result ourselves.** When the
  service *does* answer (200) it **always returns 25 fuzzy candidates, even for
  nonsense** ("asdfghjkl" → 25 results). There is no empty-list / 404 "no
  match". So correctness depends entirely on filtering the candidates:
  1. take only `Street`/`Address`-type results (city lookups: `Geoname`);
  2. **require the result's province to match the queried province.** This is
     not paranoia — a real Quebec address in the data confidently matched
     (`INTERPOLATED_POSITION`) to a same-named street in *New Brunswick*.
     Without the province check that ham would be placed in the wrong province.
     `PROVINCE_NAMES` maps the queried code to the English name geo.ca prints in
     result titles; we skip candidates whose province differs and take the
     first one that matches (so a wrong-province top hit can't mask a correct
     lower-ranked one). A wrong-province-only address becomes a miss -> city
     fallback.
  3. **for city lookups, also require the NAME and the KIND to match**
     (`pick_city`). The province check is sufficient for streets but nowhere
     near sufficient for towns: geo.ca answers a town it does not know with
     whatever same-province noise ranks first, and in a 120-query sample **565
     of the 1,141 Geoname candidates were lakes**. Measured against the old
     province-only rule, **39% of `City_Centroid` values were wrong** — 26%
     pointed at the wrong place (`Bristol's Hope, NL` → Labrador City, 1,133 km;
     `McGregor, ON` → a lake 1,168 km away; `Stayner, ON` → Stayner Lake,
     800 km) and 13% were noise that should have been NULL (`Dartmouth, NS` →
     the *Nova Scotia province centroid*; `Cheneville, QC` → geo.ca fuzzy-
     matched the literal word "province"). So a candidate must now:
     - **name the town queried**, compared through `place_key()`, which collapses
       case, diacritics, punctuation, hyphen-vs-space and Saint/Ste
       (`MONTREAL-NORD` ≡ `Montréal-Nord`, `ST JOHNS` ≡ `St. John's`,
       `STE-FOY` ≡ `Sainte-Foy`); and
     - **be a populated place** (`PLACE_KINDS`) from the title's trailing
       `(Kind)`, not a lake/river/province. `REJECT_KINDS` hard-drops
       `Province`/`Territory`/`Country`. Among same-named candidates a populated
       kind wins over a physical feature, so `Meadow Lake (City)` beats
       `Meadow Lake (Lake)`.
     Deliberately **exact, with no fuzzy tier**: prefix/token-subset matching was
     tested and rejected because the same relaxation that recovers
     `NEWCASTLE → "Newcastle Village"` also grabs `ST PETERS → "St. Peters
     Junction"` and `HUDSON'S HOPE → "Good Hope Lake"` — different places. NULL
     beats a confident wrong town.
  The `qualifier` distinguishes an exact civic match (`INTERPOLATED_POSITION`)
  from a street-centroid match (`INTERPOLATED_CENTROID`); we record it as the
  geocode_match quality but it is NOT a substitute for the province check (the
  QC->NB mismatch above was `INTERPOLATED_POSITION`).
- **Known limit — amalgamated municipalities.** A few big places are not in
  geo.ca's gazetteer under their own name because they were absorbed into a
  regional municipality; their name appears only as the *middle* (census
  division) segment of other entries. `HALIFAX, NS` and `DARTMOUTH, NS` return
  no candidate named Halifax/Dartmouth at all, so they no longer resolve at city
  level. This is not a regression in accuracy — Dartmouth's old answer was the
  *province* centroid — and it only bites rows whose *street* lookup also
  failed, which in the 2026-07-22 run was almost none: of 233 Halifax operators
  45 ended without coordinates, and of 185 Dartmouth operators **0** did (all
  street-matched). Checked the 30 cities carrying the most operators (Calgary,
  Vancouver, Edmonton, … Saskatoon): **all 30 still resolve**, so the strict rule
  costs nothing where the volume is.
- **Concurrency is throttled** per-IP: throughput plateaus at ~2.5 addr/s
  regardless of worker count (more workers just raise the 500 rate), so the run
  uses ~5 workers. First full run ≈ **7 hours** for ~60k distinct addresses;
  warm-cache reruns are minutes.

### Two-phase lookup (mirrors FCC street + centroid fallback)
- **Phase 4** — geocode every distinct `(street, city, province)` at street
  level → `coordinates`, 6-char `gridsquare`, and `geocode_match` of `Street`
  (exact civic number) or `Street_Approx` (street centroid). Province-validated.
- **Phase 5** — rows still without coordinates (PO-box / rural-route / no
  street, or a street miss) get their town's centroid via a `(city, province)`
  lookup → `geocode_match='City_Centroid'`. Province-validated like Phase 4.
  - **Accent handling:** geo.ca's place search *needs* diacritics —
    `Trois-Rivières, QC` resolves (25 Quebec hits), but the accent-stripped
    `TROIS-RIVIERES, QC` returns fuzzy garbage (Manitoba/Alberta towns) that the
    province check then rejects → a NULL. The ISED source stores the same town
    several ways (`Trois-Rivières` / `TROIS-RIVIÈRES` / `TROIS-RIVIERES` /
    `Trois-Rivieres`), so `extract_distinct_cities` collapses them on an
    **accent-insensitive key** (`city_key`, via stdlib `unicodedata`) and
    queries the variant with the MOST accents. Rows typed without accents thus
    piggyback on a correctly-accented sibling. (A town that appears *only*
    unaccented in the whole file still can't resolve — a residual limitation.)
  - **The candidate pool is every row of that town, not just the unplaced
    ones.** Those two sets differ, and the difference silently defeated the
    trick: once the rows spelled `Sept-Îles` were placed at street level, the
    only spellings still needing a centroid were the unaccented `SEPT-ILES`,
    and querying that returns nothing. Choosing the spelling from all rows
    while still querying only for the unplaced ones recovered 22 of 335 town
    queries on the 2026-07 data — `SEPT-ÎLES`, `LÉVIS`, `GASPÉ`, `GRAND-MÈRE`,
    `MONTRÉAL-NORD`, `CHÂTEAUGUAY` and friends. Spot-checked live: 4 of 5
    resolve accented and miss unaccented.

### Cache (`geocode_cache/geocode_cache.sqlite`)
Content-addressed, exactly like the FCC design: one row per normalized query
(`qkind` street/city + street/city/state), storing lat/lon/quality for hits and
a timestamped tombstone for misses. Every 100 lookups are committed
(`CACHE_FLUSH`), so an interrupted run **resumes** with at most ~100 lookups
lost — just rerun the same command. Misses older than
`--miss-retry-days` (default 30) are retried. Preserved across `--zip` reruns;
Phase 1 cleanup never touches it.

**Only real answers are cached — transient failures are not.** geo.ca has two
observable outcomes: an HTTP 200 (always a list of 25 fuzzy candidates, even for
nonsense) or an HTTP 500 (its only error, ~half of requests, random and
unrelated to the address). So a *200 whose candidates our filters reject* is a
genuine no-match and is tombstoned (`matched=0`), but a query that *never got a
200* after its retries carries no information and is **not written to the cache
at all** — `geoloc()` returns `None` for it, `street_query`/`city_query` turn
that into the `_TRANSIENT` sentinel, and `_run_pool` skips caching it. For the
current run it behaves exactly like a miss (the row falls through to the
city/FSA/province fallbacks), but because nothing was cached the **next run
re-queries it** instead of suppressing it for `--miss-retry-days`. This is the
permanent fix for what the one-off `invalidate_cache.py --misses-only` did by
hand: transient 500s no longer freeze an address at a coarse tier for a month.
`_run_pool` logs the transient count, and warns if >50 % of a phase's lookups
never answered (geo.ca degraded → rerun to fill them in).

**Cache entries outlive the rules that produced them.** `_select_todo` reuses a
cached hit forever, so tightening a picker does nothing to lookups already in
the cache — those have to be deleted so the next run re-queries them. That is a
one-off migration, kept *out* of the pipeline rather than left in it as
permanent legacy: **`invalidate_cache.py`** deletes cached entries (backing the
cache up first; `--dry-run` to preview). `--kind city|street|all` picks the
lookup type; `--misses-only` deletes just the failures (`matched=0`) and keeps
every hit. It deliberately leaves untouched whatever you don't name — re-running
the ~54k street lookups is a multi-hour job, versus ~15 minutes for the misses
alone. Run it after a picker change (whole kind) or to sweep stale failures
(`--misses-only`); on an unchanged picker it just discards good lookups. Note:
now that transient failures are no longer cached (see above), `--misses-only` is
mostly needed only to retry *real* no-matches early — the transient churn it used
to clean up no longer accumulates.

**Ctrl-C** during geocoding stops promptly: pending lookups are cancelled,
in-flight worker sleeps are aborted (a shared `_INTERRUPTED` event), the progress
so far is flushed to the cache, and the process exits (code 130) via `os._exit`
so it does not hang waiting on a worker stuck in a slow geo.ca network read.
Rerun the same command to resume from the cache.

### Coordinates -> gridsquare
`maidenhead(lat, lon, 6)` — 20°x10° field letters, 2°x1° square digits, then
2.5'x5' subsquare letters (e.g. `FN84dt`). 6 chars (vs FCC's 4) since geo.ca
gives street-level precision.

## Phase 6 — postal-code (FSA) cross-check

Both geocoder pickers can only ask whether a *name* geo.ca returned looks right,
so neither can catch the one error class that remains: a **correct street name
in the wrong town** (`312 Lakeshore Blvd, Etobicoke` → `312 Lakeshore Boulevard,
Neyaashiinigmiing`). The postal code is the only geographic evidence in the
pipeline that does not come from geo.ca, which is exactly why it can.

- Source: StatCan 2021 **Forward Sortation Area** boundaries
  (`lfsa000b21a_e.zip`, ~162 MB, EPSG:3347) — same series and machinery as the
  census-division file Phase 7 already uses. Downloaded once into the cache dir.
- Coverage: **78% of operators** carry a postal code; 1,643 FSA polygons.
- **Rule:** replace a coordinate with its FSA's interior point when the point
  lies further outside the FSA than the FSA's own radius (floor 5 km) — i.e.
  precisely when the FSA estimate is provably the closer of the two. Not an
  arbitrary threshold: urban FSAs have a median radius of **3.0 km**, rural ones
  **43.4 km**, so the same rule is strict in town and lenient in the bush, and a
  huge rural FSA can never override something better.
- Rows with a postal code but no coordinate are placed the same way, so the
  phase both **corrects and extends** coverage.
- Rows whose postal-code letter contradicts `state` (~40, the source
  disagreeing with itself) are left alone.
- `representative_point()` is used rather than `centroid`, which can fall
  outside a concave FSA.

### Boundary-file vintage (checked 2026-07-22)
**2021 is the current release** for both the FSA and census-division files.
The 2026 Census was taken in May 2026, but no 2026 geography/boundary products
exist yet — its *data* products run to Fall 2028, and boundary files follow the
census by a year or more (the 2021 files landed well after the 2021 census).
The `.../2026/...` URLs resolve, but to an error page.

That last point is a trap worth knowing: **StatCan serves a missing boundary
file as HTTP 200 with an HTML error page** (`/census-recensement/srvmsg/
srvmsg404.html`, ~4 KB), so `raise_for_status()` sees nothing wrong. Left
unchecked, a retired URL would write that HTML into the cache under a `.zip`
name, and because both loaders reuse any file that exists, *every later run*
would fail obscurely until someone deleted it by hand. `fetch_boundary_file()`
therefore verifies `zipfile.is_zipfile()` before keeping the download and
removes the temp file on any failure. Re-check these URLs when 2026 geography
is published.

Note also that Canada Post creates new FSAs over time, so a 2021 file slowly
goes stale. Phase 6 counts unrecognised FSAs and leaves those rows untouched
rather than guessing (211 rows on the 2026-07 data).

### Measured effect (run against the pre-fix database)
Corrected 9,649 coordinates and placed 1,509 previously unplaced rows.
Validated against the **city-centroid cache** — a different geo.ca query type,
validated on name+kind, so independent of the street lookups being second-
guessed:

| corrected rows, was | n | moved closer to their own town | median dist. before → after |
|---|---|---|---|
| `Street` | 1,493 | **89.5%** | 132.8 km → 6.5 km |
| `Street_Approx` | 3,766 | **89.6%** | 123.5 km → 6.5 km |

**Caveat, deliberately recorded:** the 552 corrections whose prior value was
`City_Centroid` cannot be validated this way — their coordinate *was* the city
centroid, so distance-to-city-centroid was 0 by construction and any move scores
as "farther". Overriding them is justified by the radius rule (a 3 km urban FSA
is far finer than a whole-city centroid, and an override only fires when the two
disagree by more than the FSA's radius) but it is **unproven**, and it is 0.6%
of rows. Revisit if city-level accuracy ever looks suspect.

Note these figures come from the *old* pickers' output. With the street-name and
city name/kind fixes in place there is much less for Phase 6 to correct — it is
a safety net, not the main line of defence.

## Phase 7 — province-centroid last resort

The fallback cascade for a row the street geocoder could not place, in
precedence order:
1. **postal → FSA centroid** (Phase 6) — median 3 km urban.
2. **city → city centroid** (Phase 5) — whole-town.
3. **province → province centroid** (Phase 7) — whole-province, last resort.

Phase 7 fills any row still lacking coordinates that has a `state`, using an
interior point of that province unioned from the census-division polygons
(reuses the Phase 8 file, no extra download; `representative_point`, so it is
always inside the province, verified: 0 of 19,973 fell outside). Labelled
`Province_Centroid`.

It is deliberately coarse — it carries no more information than the `state`
column already does — so it is **excluded from county** (Phase 8) and therefore
from the Ontario section split: a whole-province point would otherwise land in
one arbitrary census division and hand the row a wrong county and wrong ON
section. Non-Ontario rows still get their RAC section, which is a whole-province
value regardless. On the 2026-07 data this placed 19,973 rows, leaving only the
1,642 rows with no province at all NULL.

**Ordering caveat (worth a decision):** the cascade currently runs city
(Phase 5) *before* postal (Phase 6), so a row with a resolvable city and a valid
FSA keeps the coarser `City_Centroid` unless the postal check finds it contradicts
the FSA (> its radius). Strict "postal first" would instead give it the tighter
urban FSA point. Phase 6's correction step already fixes the *wrong* city
placements, so this only costs precision (town vs ~3 km) on rows where city and
FSA agree — not correctness. Flipping Phase 5/6 would honour the precise
precedence; left as-is for now.

### geocode_match values
`Street` (exact civic match), `Street_Approx` (street centroid — right street,
civic number not pinned), `FSA_Centroid` (interior point of the postal code's
Forward Sortation Area — Phase 6; median radius 3.0 km urban, 43.4 km rural),
`City_Centroid` (town centroid fallback — the result must name the town and be a
populated place, not just sit in the right province), `Province_Centroid`
(whole-province interior point — Phase 7, last resort), NULL (no address at all,
not even a province).

Rough precision ordering: `Street` > `Street_Approx` ≈ `FSA_Centroid` (urban) >
`City_Centroid` > `FSA_Centroid` (rural) > `Province_Centroid`. An urban FSA is
considerably tighter than a whole city, which is why Phase 6 is allowed to
override a city centroid. Anything consuming `coordinates` for anything finer
than "which province" should filter out `Province_Centroid`.

## Step 4 status: DONE (ARRL/RAC section)
`update_ca_db.py` Phase 9 fills a Canada-only `arrl_section` column (column 39;
columns 1-38 stay byte-identical to FCC). Value = the RAC section used in ARRL
contests (Sweepstakes, Field Day, 160 m).

### Mapping — Phase 9 is a pure lookup (no geometry of its own)
The section is derived, in order: **county is computed first (Phase 6), then the
section is just a lookup.** Phase 9 does zero point-in-polygon — it reads the
census division Phase 6 already resolved.
- **12 of 13 province codes map 1:1 or many:1** via `SECTION_BY_PROVINCE`
  (`state` code → section): NL, NS, NB, PE, QC, MB, SK, AB, BC each to their
  own; **YT + NT + NU all share `TER` (Territories)** — Yukon, NWT, Nunavut
  (renamed from "Northern Territories" in 2023). 250+108+40 = 398 rows.
- **Ontario** (4 sections: GH / ONE / ONN / ONS) can't be resolved from the `ON`
  code, so it's looked up from the **`county`** (census division) Phase 6 wrote:
  `ON_COUNTY_SECTION[county]`, a name-keyed dict of all 49 Ontario divisions per
  RAC's official "Ontario Sections effective 01 Jan 2023" list. This reuses the
  point-in-polygon Phase 6 already did instead of repeating it (`county` first,
  section is a `UPDATE ... WHERE state='ON' AND county=?`). The 49 keys are
  verified to exactly equal the boundary file's Ontario division names.
- Ontario rows with a **NULL county** (no coordinates, or `--no-county`), and
  blank-province rows, stay NULL. So Phase 9's Ontario output depends on Phase 6.

### Authoritative source & gotchas
RAC "Ontario Sections effective 01 Jan 2023" ([va3cco PDF](https://www.va3cco.com/ontariosections2023.pdf),
[RAC 2023 changes](https://www.rac.ca/changes-to-the-rac-field-organization-effective-january-1-2023/)):
- **Hamilton and Niagara are GH**, not ONS (moved in 2019 — older maps show ONS).
- **Kawartha Lakes is ONE** (not ONS as its central location suggests); Muskoka
  *is* ONS. Verified against the official list, not guessed.
- **Nipissing is split** by Algonquin Park (north=ONN, south=ONE). Its only
  populated area (North Bay) is in the ONN part, so the whole division is
  treated as ONN — the south-of-Algonquin sliver is uninhabited parkland.

### CRS note (applies to Phase 6, which does the point-in-polygon)
StatCan boundary files are projected (**NAD83 Statistics Canada Lambert,
EPSG:3347**, metres) — unlike the FCC's lat/lon Census files. Phase 6 reprojects
each row's WGS-84 coordinate to EPSG:3347 (via `pyproj`) before the
point-in-polygon test, rather than reprojecting the polygons. Phase 9 (section)
needs no geometry libs at all — it's pure SQL off `state` and `county`.

FCC parity: `arrl_section` is Canada-only (the FCC pipeline is being handled
separately). Deps `shapely`/`pyshp`/`pyproj` are needed only for Phase 6.

New flags: `--cache-dir`, `--workers` (default 8; use ~5 in practice),
`--limit N` (test on N street addresses), `--miss-retry-days`, `--no-geocode`.
