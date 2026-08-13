"""Postgres storage for fire detection history.

Every reload appends the freshly fetched detections here, so this table is
the durable history of everything that has ever been pulled from FIRMS.
Valkey (see `cache.py`) holds only the *current* snapshot.
"""

import logging
import uuid
from collections.abc import Iterable
from typing import Any

from geojson import Feature
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import POSTGRES_DSN

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None

# Needed for the geography column + ST_DWithin query below
# (get_history_in_area) -- ships in the postgis/postgis image this app's
# docker-compose.yaml now uses instead of plain postgres.
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS fire_detections (
    id BIGSERIAL PRIMARY KEY,
    scan_id UUID NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    bright_ti4 DOUBLE PRECISION,
    bright_ti5 DOUBLE PRECISION,
    scan DOUBLE PRECISION,
    track DOUBLE PRECISION,
    frp DOUBLE PRECISION,
    acq_datetime TIMESTAMPTZ NOT NULL,
    satellite TEXT,
    instrument TEXT,
    confidence TEXT,
    version TEXT,
    daynight TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fire_detections_scan_id ON fire_detections (scan_id);
CREATE INDEX IF NOT EXISTS idx_fire_detections_fetched_at ON fire_detections (fetched_at);
CREATE INDEX IF NOT EXISTS idx_fire_detections_acq_datetime ON fire_detections (acq_datetime);
-- Added for the area-history query -- a generated column (rather than
-- something maintained by the app on every insert) so it's always in sync
-- with latitude/longitude, and ADD COLUMN IF NOT EXISTS so this is safe to
-- rerun against a table created before PostGIS was added. geography (not
-- geometry) so ST_DWithin below takes a plain meters radius instead of
-- degrees.
ALTER TABLE fire_detections ADD COLUMN IF NOT EXISTS geom GEOGRAPHY(Point, 4326)
    GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography) STORED;
CREATE INDEX IF NOT EXISTS idx_fire_detections_geom ON fire_detections USING GIST (geom);
"""

INSERT = """
INSERT INTO fire_detections (
    scan_id, latitude, longitude, bright_ti4, bright_ti5,
    scan, track, frp, acq_datetime, satellite, instrument,
    confidence, version, daynight
) VALUES (
    %(scan_id)s, %(latitude)s, %(longitude)s, %(bright_ti4)s, %(bright_ti5)s,
    %(scan)s, %(track)s, %(frp)s, %(acq_datetime)s, %(satellite)s, %(instrument)s,
    %(confidence)s, %(version)s, %(daynight)s
);
"""


def get_pool() -> ConnectionPool:
    """Lazily create (and reuse) the connection pool."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo=POSTGRES_DSN, min_size=1, max_size=5, open=True)
    return _pool


def init_db() -> None:
    """Create the history table if it doesn't already exist."""
    logger.info("Ensuring fire_detections table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def feature_to_row(feature: Feature, scan_id: uuid.UUID) -> dict[str, Any]:
    """Flatten a GeoJSON Feature (as produced by get_fire_data) into a DB row.

    `scan_id` ties every row from the same reload together — it's a UUIDv7,
    so it's also naturally sortable by when the scan was collected.
    """
    longitude, latitude = feature["geometry"]["coordinates"]
    props = feature["properties"]
    return {
        "scan_id": scan_id,
        "latitude": latitude,
        "longitude": longitude,
        "bright_ti4": float(props["bright_ti4"]),
        "bright_ti5": float(props["bright_ti5"]),
        "scan": float(props["scan"]),
        "track": float(props["track"]),
        "frp": float(props["frp"]),
        "acq_datetime": props["datetime"],
        "satellite": props["satellite"],
        "instrument": props["instrument"],
        "confidence": props["confidence"],
        "version": props["version"],
        "daynight": props["daynight"],
    }


def insert_detections(
    features: Iterable[Feature],
    scan_id: uuid.UUID | None = None,
) -> tuple[int, uuid.UUID]:
    """Insert a batch of detections as a single scan. Returns (row count, scan_id)."""
    scan_id = scan_id or uuid.uuid7()
    rows = [feature_to_row(feature, scan_id) for feature in features]

    if not rows:
        return 0, scan_id

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(INSERT, rows)
        conn.commit()

    logger.info(f"Inserted {len(rows)} fire detections into Postgres ({scan_id=})")
    return len(rows), scan_id


# Every column except `geom` -- it's WKB, not something callers of either
# get_history() or get_history_in_area() need back (they already get
# latitude/longitude), and not worth teaching the response models to skip.
_DETECTION_COLUMNS = sql.SQL(
    "id, scan_id, latitude, longitude, bright_ti4, bright_ti5, scan, track, "
    "frp, acq_datetime, satellite, instrument, confidence, version, "
    "daynight, fetched_at"
)


def get_history(
    limit: int = 500,
    since: str | None = None,
    scan_id: uuid.UUID | str | None = None,
) -> list[dict[str, Any]]:
    """Read historical detections back out of Postgres, most recent first.

    Built from `sql.SQL()` fragments (each one a fixed literal, never
    caller-supplied text) rather than plain string concatenation --
    psycopg's `execute()` wants a `LiteralString | SQL | Composed`, not a
    str assembled at runtime, and this is also just the idiomatic way to
    compose a dynamic query with this library.
    """
    clauses: list[sql.Composable] = []
    params: dict[str, Any] = {}

    if since:
        clauses.append(sql.SQL("fetched_at >= %(since)s"))
        params["since"] = since

    if scan_id:
        clauses.append(sql.SQL("scan_id = %(scan_id)s"))
        params["scan_id"] = str(scan_id)

    query = sql.SQL("SELECT {} FROM fire_detections").format(_DETECTION_COLUMNS)
    if clauses:
        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)

    query += sql.SQL(" ORDER BY fetched_at DESC, id DESC LIMIT %(limit)s")
    params["limit"] = limit

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


MILES_TO_METERS = 1609.34


def get_history_in_area(
    latitude: float,
    longitude: float,
    radius_miles: float,
    limit: int = 500,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Read historical detections within `radius_miles` of a point, nearest
    first, one row per physical detection -- the query behind the map's
    "show history here" button (see /history/area) and per-subscriber
    history (see /subscribers/{id}/history).

    FIRMS' rolling window hands back the same physical detection (same
    point, same acquisition time, same satellite) across several
    consecutive reloads before it ages out of that window -- see
    notification_log.py's dedup docstring and cache.py's event_id, which
    key on exactly this. fire_detections stores every one of those reloads
    as its own row (that's the point of a history table), so a naive query
    here would show one real fire as several duplicate points. ROW_NUMBER
    collapses each (latitude, longitude, acq_datetime, satellite) group
    down to the row from its earliest reload before the radius/limit are
    ever applied.

    Uses the generated `geom` geography column (see SCHEMA above) with
    ST_DWithin, which can use the GIST index unlike computing distance for
    every row up front -- the same reason subscribers.py's radius matching
    doesn't try to do this in Postgres (that table has no PostGIS column at
    all yet, and matches far fewer rows per check).
    """
    clauses: list[sql.Composable] = [
        sql.SQL(
            "ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, "
            "%(radius_m)s)"
        )
    ]
    params: dict[str, Any] = {
        "lon": longitude,
        "lat": latitude,
        "radius_m": radius_miles * MILES_TO_METERS,
        "limit": limit,
    }

    if since:
        clauses.append(sql.SQL("fetched_at >= %(since)s"))
        params["since"] = since

    query = (
        sql.SQL("WITH matches AS (SELECT {}, ").format(_DETECTION_COLUMNS)
        + sql.SQL(
            "ST_Distance(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography) "
            "/ 1609.34 AS distance_miles, "
            "ROW_NUMBER() OVER ("
            "PARTITION BY latitude, longitude, acq_datetime, satellite "
            "ORDER BY fetched_at ASC"
            ") AS detection_rank "
            "FROM fire_detections WHERE "
        )
        + sql.SQL(" AND ").join(clauses)
        + sql.SQL(") SELECT * FROM matches WHERE detection_rank = 1 ")
        + sql.SQL("ORDER BY distance_miles ASC, fetched_at DESC LIMIT %(limit)s")
    )

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    for row in rows:
        del row["detection_rank"]
    return rows


def get_scans(limit: int = 50) -> list[dict[str, Any]]:
    """List recent scans (one row per reload), for grouping history by collection event."""
    query = """
        SELECT
            scan_id,
            min(fetched_at) AS fetched_at,
            count(*) AS detection_count
        FROM fire_detections
        GROUP BY scan_id
        ORDER BY fetched_at DESC
        LIMIT %(limit)s
    """

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, {"limit": limit})
        return cur.fetchall()
