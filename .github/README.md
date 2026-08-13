# Fire Map

Display Fire Data from Around the World using NASA's Fire Information for
Resource Management System (FIRMS) API.

## Architecture

The whole stack runs via Docker Compose:

- **postgres** — durable history. Every reload appends its detections to the
  `fire_detections` table, so this is a full timeline of everything FIRMS has
  ever reported.
- **valkey** — a cache of the *current* snapshot only. It's written with a
  TTL equal to `UPDATE_INTERVAL_SECONDS`, so it expires right around the time
  the next reload is due.
- **web** — a FastAPI app that serves the map (reading the current snapshot
  from Valkey) and the API.
- **scheduler** — a small loop that `POST`s `/reload` every
  `UPDATE_INTERVAL_SECONDS`.

## Running it

```sh
cp .env.example .env   # fill in NASA_API_KEY at minimum
mise run up             # or: docker compose up --build
```

## API

- `GET /` — map, backed by the current Valkey snapshot.
- `POST /reload` — fetches fresh global data from FIRMS, appends it to
  Postgres, refreshes the Valkey snapshot, and returns what was just cached
  (read back from Valkey).
- `GET /current` — the current snapshot from Valkey (auto-reloads once if
  the cache has expired).
- `GET /history?limit=&since=&scan_id=` — historical detections read
  straight from Postgres, optionally scoped to one scan.
- `GET /scans?limit=` — recent scans (one entry per reload) with detection
  counts, for grouping history by collection event.

Fire data is pulled globally (FIRMS' `area` API with `world` as the bounding
box) rather than per-country — the API dropped country attribution when NASA
retired the old country-based endpoint, so there's no reliable way to filter
by country without a separate reverse-geocoding step.

## Local development

This project uses [mise](https://mise.jdx.dev) for tool/task management and
[uv](https://docs.astral.sh/uv/) for Python dependencies, targeting
Python 3.14.

```sh
mise install
mise run install
mise run dev     # runs the web app directly; needs postgres/valkey reachable
```
