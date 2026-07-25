# POTA Park List Downloader

Downloads the Parks on the Air park list. Unlike the FCC and Canada downloaders,
there is **no build step** — POTA publishes the list as a CSV, and that CSV is
what the consumer reads, so this script only fetches and verifies it.

## Run

```
python update_pota_parks.py
```

Stdlib only — no virtualenv and no `requirements.txt`, unlike its siblings.
Takes a few seconds. Exit code is 0 on success, 1 on failure.

## Source

<https://pota.app/all_parks_ext.csv> — regenerated daily, ~9 MB.

Columns: `reference, name, active, entityId, locationDesc, latitude, longitude, grid`.
There is also an `all_parks.csv` (same, minus coordinates and grid) which this
script deliberately does not use.

As of 2026-07-24 the file holds 93,719 parks, 89,580 of them active, across 238
prefixes. Note the list is not US-centric: France (16,301) has more entries than
the US (13,360).

## Output

`all_parks_ext.csv`, written next to the script.

The download lands in `all_parks_ext.csv.part` and is renamed over the previous
copy only after the header carries every required column and the row count clears
`MIN_ROWS` — so a truncated download or an HTML error page can never replace a
good file. A failed run leaves the previous copy untouched and says so. Extra
columns upstream are accepted (consumers read by header name); a missing one is
fatal.

`MIN_ROWS` is 50,000 against a real count of ~93,700. Raise it if you want a
tighter tripwire as the list grows.
