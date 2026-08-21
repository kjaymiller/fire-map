---
title: Fixing Fire Map's slow homepage — from 6.3s to 5ms
status: draft
---

I kept hearing "the fire map site takes forever to load" and assumed
it was an async problem — some blocking call in FastAPI tying up a
worker. It wasn't. The real causes were more boring, and the fix that
mattered most wasn't even the one I found first.

## Starting point: 6.36 seconds for the homepage

Before touching anything, `GET /` on the homepage was taking **6.36
seconds**. For a page that's mostly a map and a handful of stats, that's
bad. I went looking for where the time was actually going instead of
guessing.

## Culprit 1: embedding the whole live dataset inline

The homepage route was doing this on every request:

1. Read the entire current snapshot of fire detections out of Valkey
   (a `GET` plus an `MGET` of every cached detection key).
2. Hand the whole thing to Jinja.
3. Serialize it straight into the page as
   `var points = {{ points | tojson }};`.

Nothing on the page — not the header, not the map tiles, nothing —
could render until all of that had finished and made it down the wire
as part of the initial HTML. The app already had a `/current` JSON
endpoint serving this same data, so the fix was to use it: the homepage
route stopped embedding the dataset, and `map.html`'s JS now builds the
map shell first (tiles, controls, an explicit loading overlay with a
spinner) and fetches `/current` in the background afterward, dropping
markers in once they arrive.

```js
map.addLayer(primaryMarkers); // empty, but the map's already up

fetch('/current', { credentials: 'same-origin' })
    .then(function (response) { return response.json(); })
    .then(function (points) {
        renderDetections(points);
        renderStats(points);
        hideMapLoadingOverlay();
    })
    .catch(function (err) {
        console.error('Failed to load fire detections:', err);
        hideMapLoadingOverlay();
    });
```

That got the page feeling responsive — the shell painted immediately —
but the *server response itself* was still slow, because of the next
two things.

## Culprit 2: a GROUP BY that scaled with the whole table's history

The "Latest run: X detections" line came from a query that grouped
every row in `fire_detections` by `scan_id` to find the most recent
one:

```sql
SELECT scan_id, min(fetched_at), count(*)
FROM fire_detections
GROUP BY scan_id
ORDER BY fetched_at DESC
LIMIT 1
```

`fire_detections` is the durable history of every fire detection this
app has ever pulled from NASA FIRMS, on a 15-minute cycle, forever. My
first fix flipped the query to find recent scan_ids off the indexed
`fetched_at` column first, then aggregate only those rows, instead of
grouping the whole table. It helped, but nowhere near enough — and
digging into why exposed the real shape of the problem.

## Culprit 3 (the actual big one): scans aren't small

I checked what was actually in the table:

```
count(*) FROM fire_detections            -> 20,673,992 rows
count(DISTINCT scan_id) FROM fire_detections -> 388 scans
```

Twenty **million** rows across 388 scans — an average of over 53,000
rows *per scan*, because this pulls global VIIRS fire data, not a
single region. My "smarter query" from culprit 2 was still built
around aggregating `fire_detections` directly, and any query that does
that has to read on the order of (rows-per-scan × scans-requested)
rows to find the last N distinct scans. With scans this big, that's
still hundreds of thousands of rows touched just to answer "what's the
latest scan" — which is exactly why `/scans?limit=1` alone was taking
**4.9 seconds**.

The actual fix was to stop deriving scan metadata from
`fire_detections` at read time at all. I added a tiny `scans` summary
table — one row per reload, written alongside the detection rows
instead of computed from them afterward:

```sql
CREATE TABLE scans (
    scan_id UUID PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL,
    detection_count INTEGER NOT NULL
);
```

`insert_detections` now writes one row here per scan at insert time,
and `get_scans` just reads it:

```sql
SELECT scan_id, fetched_at, detection_count
FROM scans
ORDER BY fetched_at DESC
LIMIT %(limit)s
```

Existing scans that predated this table got backfilled once, on
startup, from the same `GROUP BY` this was replacing — a one-time cost
against 20 million rows, instead of a recurring one on every homepage
load.

## Culprit 4: a redundant second read of the same data

With `/current` handling the map's data and `scans` handling the
"latest run" line, there was one thing left: the homepage route was
*still* independently reading the entire live snapshot out of Valkey,
just to compute a total count and a confidence-level breakdown — the
same two numbers the client was about to derive from its own
`/current` fetch anyway. I moved that computation to the client too
(`renderStats()`, run right alongside `renderDetections()` off the same
fetch), and deleted it from the server route entirely. `index()` now
does exactly one thing: read the one cheap row from `scans`, and
render the shell.

## Before and after

| | Before | After |
|---|---|---|
| `GET /` (homepage) | 6.36s | ~0.005s |
| `GET /scans?limit=1` | 4.90s | ~0.002s |
| `GET /current` (async, client-side, off the critical path) | n/a (was inlined) | ~0.9s |

The homepage went from 6.36 seconds to low single-digit milliseconds.
The live detection data (still a genuinely large payload — 93,601
points in the current snapshot) now loads asynchronously behind a
loading overlay instead of blocking the page, and it takes about the
same ~0.9s it always did, because that part actually is real work: a
Valkey read of tens of thousands of keys. The difference is the user
isn't stuck waiting on it before they see anything.

## What I'd take from this

None of this turned out to be an "add async" problem — FastAPI already
runs synchronous routes in a thread pool, so one slow request wasn't
blocking others. The real issues were:

- A query whose cost scaled with the table's entire lifetime instead
  of recent activity.
- An assumption ("a scan is a modest number of rows") that didn't hold
  for this dataset, which quietly turned my first "fix" into something
  still too slow to matter.
- A page that made the user wait on the *entire* payload — twice, once
  server-side and once client-side — before showing anything.

The lesson that actually stuck: measure the real shape of the data
before trusting a query plan's intuition. "388 scans" sounds small.
"20.6 million rows averaging 53,000 per scan" is a completely different
problem, and no amount of clever SQL against the wrong table structure
was going to fix it — only writing the summary down once, up front,
did.
