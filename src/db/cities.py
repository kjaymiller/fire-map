"""Postgres storage for towns/cities, used to label a raw (lat, lon) fire
detection with the nearest place name.

Loaded from a GeoNames (https://www.geonames.org/) cities dump rather than
queried live against an external geocoder -- the lookup happens once per
matched detection (see notify.py), and a local table with a PostGIS index
answers that in one query with no network round-trip, no rate limit, and no
API key to manage. Loading the dataset is a separate, explicit step (see
load_geonames / the `load-cities` mise task) -- it's a one-time ~30MB
download, not something every app startup should trigger.
"""

import csv
import logging
import zipfile
from collections.abc import Iterable
from io import BytesIO, TextIOWrapper
from typing import Any

import httpx
from psycopg.rows import dict_row

from . import NEAREST_TOWN_MAX_DISTANCE_MILES
from .postgres import MILES_TO_METERS, get_pool

logger = logging.getLogger(__name__)

# cities500 (population >= 500) rather than cities5000/cities15000 -- "local
# towns", not just major cities, is the point. Override via env if a
# smaller/larger dump is preferred.
DEFAULT_GEONAMES_URL = "https://download.geonames.org/export/dump/cities500.zip"

# Needed for the geography column + KNN query below, same as
# fire_detections' own `geom` column (see postgres.py).
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
-- Lets search_cities fall back to fuzzy (trigram) matching, so a
-- misspelled search ("Sprinfield") still finds "Springfield" instead of
-- coming back empty the way a pure prefix match would.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- levenshtein() below, used alongside pg_trgm to rank search_cities'
-- results -- trigram similarity alone is noisy for short names (see that
-- function's docstring), and plain edit distance is exactly the metric
-- that's actually reliable there.
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
CREATE TABLE IF NOT EXISTS cities (
    geoname_id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    country_code TEXT,
    admin1_code TEXT,
    population BIGINT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL
);
ALTER TABLE cities ADD COLUMN IF NOT EXISTS geom GEOGRAPHY(Point, 4326)
    GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography) STORED;
CREATE INDEX IF NOT EXISTS idx_cities_geom ON cities USING GIST (geom);
-- Backs search_cities' prefix match -- text_pattern_ops (rather than the
-- default btree opclass) is what actually lets a `LIKE 'foo%'` query use
-- this index; a plain btree index on lower(name) only helps equality
-- lookups. Kept alongside the trigram index below: a correct prefix still
-- gets the fast, exact-ranked path, while pg_trgm only has to cover the
-- fuzzy/misspelled fallback.
CREATE INDEX IF NOT EXISTS idx_cities_name_lower ON cities (lower(name) text_pattern_ops);
-- Backs search_cities' trigram fallback (the `<%` word-similarity operator
-- and `word_similarity()` ordering below) -- GIN over GiST since this
-- index is read-heavy and rarely written to (only ever rebuilt by
-- load_geonames). gin_trgm_ops covers word_similarity's operators the same
-- way it covers plain similarity's.
CREATE INDEX IF NOT EXISTS idx_cities_name_trgm ON cities USING GIN (name gin_trgm_ops);
"""

UPSERT = """
INSERT INTO cities (geoname_id, name, country_code, admin1_code, population, latitude, longitude)
VALUES (%(geoname_id)s, %(name)s, %(country_code)s, %(admin1_code)s, %(population)s,
        %(latitude)s, %(longitude)s)
ON CONFLICT (geoname_id) DO UPDATE SET
    name = EXCLUDED.name,
    country_code = EXCLUDED.country_code,
    admin1_code = EXCLUDED.admin1_code,
    population = EXCLUDED.population,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude;
"""

# Nearest neighbor via the `<->` KNN operator, which (unlike ST_DWithin's
# ST_Distance ordering) can actually use the GIST index -- fine here since
# this table is much smaller than fire_detections, but there's no reason not
# to use the same trick. The row still comes back with its real distance so
# the caller can reject it if it's farther than NEAREST_TOWN_MAX_DISTANCE_MILES.
NEAREST_CITY_QUERY = """
SELECT
    geoname_id, name, country_code, admin1_code, population, latitude, longitude,
    ST_Distance(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography)
        / %(meters_per_mile)s AS distance_miles
FROM cities
ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
LIMIT 1;
"""


def init_db() -> None:
    """Create the cities table if it doesn't already exist. Doesn't load
    any data -- see load_geonames for that.
    """
    logger.info("Ensuring cities table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def _parse_geonames_rows(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Parse GeoNames' tab-delimited cities dump into rows for `cities`.

    Columns (see https://download.geonames.org/export/dump/readme.txt):
    geonameid, name, asciiname, alternatenames, latitude, longitude,
    feature class, feature code, country code, cc2, admin1 code, admin2
    code, admin3 code, admin4 code, population, elevation, dem, timezone,
    modification date. Only the columns this app actually uses are kept.
    """
    reader = csv.reader(lines, delimiter="\t")
    for row in reader:
        if len(row) < 15:
            continue
        yield {
            "geoname_id": int(row[0]),
            "name": row[1],
            "latitude": float(row[4]),
            "longitude": float(row[5]),
            "country_code": row[8] or None,
            "admin1_code": row[10] or None,
            "population": int(row[14]) if row[14] else None,
        }


def load_geonames(url: str = DEFAULT_GEONAMES_URL, batch_size: int = 5000) -> int:
    """Download a GeoNames cities dump and upsert every row into `cities`.

    Safe to rerun -- ON CONFLICT (geoname_id) DO UPDATE means a later run
    with a fresher dump just refreshes names/populations in place, it
    doesn't duplicate rows.
    """
    logger.info(f"Downloading GeoNames cities dump from {url}")
    response = httpx.get(url, timeout=120, follow_redirects=True)
    response.raise_for_status()

    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        # The dump's one .txt member shares the zip's own basename.
        (name,) = [n for n in archive.namelist() if n.endswith(".txt")]
        with archive.open(name) as raw:
            lines = TextIOWrapper(raw, encoding="utf-8")

            total = 0
            batch: list[dict[str, Any]] = []
            with get_pool().connection() as conn:
                for city_row in _parse_geonames_rows(lines):
                    batch.append(city_row)
                    if len(batch) >= batch_size:
                        with conn.cursor() as cur:
                            cur.executemany(UPSERT, batch)
                        conn.commit()
                        total += len(batch)
                        logger.info(f"Loaded {total} cities so far")
                        batch = []

                if batch:
                    with conn.cursor() as cur:
                        cur.executemany(UPSERT, batch)
                    conn.commit()
                    total += len(batch)

    logger.info(f"Loaded {total} cities from {url}")
    return total


def nearest_city(
    latitude: float,
    longitude: float,
    max_distance_miles: float = NEAREST_TOWN_MAX_DISTANCE_MILES,
) -> dict[str, Any] | None:
    """The closest row in `cities` to (latitude, longitude), or None if
    either the table is empty or the closest row is farther than
    `max_distance_miles` away (open ocean, remote wilderness, etc.).
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            NEAREST_CITY_QUERY,
            {"lat": latitude, "lon": longitude, "meters_per_mile": MILES_TO_METERS},
        )
        row = cur.fetchone()

    if row is None or row["distance_miles"] > max_distance_miles:
        return None
    return row


def search_cities(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Cities matching `query`, best match first -- backs the city-alert
    subscribe form's and the map's search boxes, so someone can find
    "Springfield" without already knowing its geoname_id.

    Candidates come from either of two match modes:
      - a plain prefix match (fast, via idx_cities_name_lower) -- keeps the
        common case (someone correctly typing the start of a real name)
        ranked first and exact, the same as before this had any fuzzy
        matching at all;
      - a trigram *word* similarity match (pg_trgm's `<%` operator, via
        idx_cities_name_trgm) -- catches everything a strict prefix would
        miss, most importantly a misspelling ("Sprinfield" still finds
        "Springfield").

    Ranking a surviving candidate is a separate question from matching it,
    and needs a different metric: word_similarity scores the query against
    whichever word-boundary substring of `name` matches best, which is
    great for a short query against a longer multi-word name ("New Yrok"
    still finds "New York City") but is *noisy* for a name close to the
    query's own length -- a handful of shared trigrams is a bigger fraction
    of a short name's total, so trigram scoring systematically over-ranks
    obscure short names ("Alta", "Anta") over the actual likely target
    ("Atlanta") for a query like "Altanta". Plain edit distance
    (levenshtein, normalized into a 0-1 score by string length) doesn't
    have that bias and is exactly the right metric for that same-length
    case. Neither metric is reliable on its own across both shapes of
    match, so every candidate is scored by whichever of the two rates it
    higher, and the result sorts by that.

    Ordered prefix-matches-first, then by that composite score, then by
    population as the final tiebreaker. Empty list for a blank query
    rather than the whole table.
    """
    query = query.strip()
    if not query:
        return []

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # pg_trgm's own default for the `<%` operator (0.6) is tuned for
        # longer documents and is too strict for short city names -- at
        # that default, a plain one-letter typo like "Atlnta" scores 0.5
        # and gets filtered out before ORDER BY ever sees it. Lowered to
        # match plain similarity's own threshold (0.3), which is what
        # actually lets the misspellings this function exists for surface
        # at all. Session-scoped (not LOCAL/transaction-scoped) since
        # every caller through this pool wants the same lowered threshold.
        cur.execute("SET pg_trgm.word_similarity_threshold = 0.3")
        cur.execute(
            "SELECT geoname_id, name, country_code, admin1_code, population, "
            "latitude, longitude, "
            "GREATEST("
            "1 - levenshtein(lower(name), lower(%(query)s))::float "
            "/ greatest(length(name), length(%(query)s)), "
            "word_similarity(%(query)s, name)"
            ") AS score "
            "FROM cities "
            "WHERE lower(name) LIKE lower(%(prefix)s) OR %(query)s <%% name "
            "ORDER BY (lower(name) LIKE lower(%(prefix)s)) DESC, "
            "score DESC, population DESC NULLS LAST "
            "LIMIT %(limit)s",
            {"query": query, "prefix": f"{query}%", "limit": limit},
        )
        return cur.fetchall()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    init_db()
    load_geonames(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GEONAMES_URL)
