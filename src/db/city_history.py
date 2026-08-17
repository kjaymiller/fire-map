"""Postgres storage for the full history of fire detections near each city.

Every scan, notify.py resolves each detection's nearest city
(cities.nearest_city) and records it here -- unconditionally, regardless of
whether that detection happened to match any subscriber -- so this is the
durable "what's this city been dealing with" log behind
/towns/{geoname_id}/history, not just the detections that triggered an
alert for someone.

This is deliberately a plain Postgres table, not a Valkey cache with a
TTL: the whole point is a *full* history that keeps growing, the same way
fire_detections does for the whole map, rather than a rolling window that
forgets anything older than a few days.
"""

import logging
import uuid
from typing import Any

from geojson import Feature
from psycopg.rows import dict_row

from .postgres import get_pool

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS city_fire_history (
    id BIGSERIAL PRIMARY KEY,
    geoname_id BIGINT NOT NULL REFERENCES cities(geoname_id) ON DELETE CASCADE,
    scan_id UUID NOT NULL,
    detection_key TEXT NOT NULL,
    distance_miles DOUBLE PRECISION NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    frp DOUBLE PRECISION,
    confidence TEXT,
    satellite TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_city_fire_history_city
    ON city_fire_history (geoname_id, detected_at DESC);
-- Same rationale as notification_log's dedup index -- FIRMS' rolling
-- window can hand back the same physical detection across several
-- consecutive reloads before it ages out (see cache.py's event_id); a
-- city's history should show it once, not once per reload it was still in
-- that window.
CREATE UNIQUE INDEX IF NOT EXISTS idx_city_fire_history_dedup
    ON city_fire_history (geoname_id, detection_key);
"""

INSERT = """
INSERT INTO city_fire_history (
    geoname_id, scan_id, detection_key, distance_miles, detected_at,
    latitude, longitude, frp, confidence, satellite
) VALUES (
    %(geoname_id)s, %(scan_id)s, %(detection_key)s, %(distance_miles)s, %(detected_at)s,
    %(latitude)s, %(longitude)s, %(frp)s, %(confidence)s, %(satellite)s
)
ON CONFLICT (geoname_id, detection_key) DO NOTHING;
"""


def init_db() -> None:
    """Create the city_fire_history table if it doesn't already exist.

    Depends on the cities table already existing (FK) -- call
    cities.init_db() first.
    """
    logger.info("Ensuring city_fire_history table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def record_detection(
    town: dict[str, Any],
    feature: Feature,
    scan_id: uuid.UUID | str,
    distance_miles: float,
    detection_key: str,
) -> None:
    """Log one detection against `town`'s history.

    `town` is a row from cities.nearest_city -- only its geoname_id is used
    here. Idempotent: ON CONFLICT DO NOTHING against (geoname_id,
    detection_key) means calling this again for a detection FIRMS keeps
    handing back across later reloads just no-ops.
    """
    props = feature["properties"]
    longitude, latitude = feature["geometry"]["coordinates"]
    with get_pool().connection() as conn:
        conn.execute(
            INSERT,
            {
                "geoname_id": town["geoname_id"],
                "scan_id": str(scan_id),
                "detection_key": detection_key,
                "distance_miles": round(distance_miles, 1),
                "detected_at": props["datetime"],
                "latitude": latitude,
                "longitude": longitude,
                "frp": props.get("frp"),
                "confidence": props.get("confidence"),
                "satellite": props.get("satellite"),
            },
        )
        conn.commit()


def get_history(
    geoname_id: int, limit: int = 500, since: str | None = None
) -> list[dict[str, Any]]:
    """Full history of detections logged near this city, most recent first.

    Not windowed by any TTL -- see the module docstring. Empty (not an
    error) once nothing's ever been logged for this geoname_id, whether or
    not it's actually a real one.
    """
    clauses = ["geoname_id = %(geoname_id)s"]
    params: dict[str, Any] = {"geoname_id": geoname_id, "limit": limit}
    if since:
        clauses.append("detected_at >= %(since)s")
        params["since"] = since

    query = (
        "SELECT * FROM city_fire_history WHERE "
        + " AND ".join(clauses)
        + " ORDER BY detected_at DESC LIMIT %(limit)s"
    )
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()
