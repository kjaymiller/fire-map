"""Postgres storage for city-based fire alert subscriptions.

Alongside subscribers.py's point + radius subscriptions, this lets someone
subscribe to a whole city (a row in `cities`, see cities.py) instead of a
hand-picked point. By default this is matched in notify.py by comparing a
detection's resolved nearest city (cities.nearest_city) against this
table's geoname_id -- no radius, just "this city". Setting `radius_miles`
switches that subscription to matching by distance from the city's own
point instead (same as subscribers.py), which also catches detections
whose nearest city resolved to somewhere else nearby.

Kept as its own table (rather than folding into subscribers) since the two
have genuinely different shapes -- one keys off a point, the other off a
geoname_id -- and different match logic in notify.py. `subscriptions_view`
below is where they're brought back together for callers (notification_log,
/manage) that just want "everything this owner has configured" without
caring which kind.
"""

import logging
from typing import Any

from psycopg.rows import dict_row

from .postgres import get_pool

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS city_subscribers (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    contact TEXT NOT NULL,
    geoname_id BIGINT NOT NULL REFERENCES cities(geoname_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One subscription per (owner, city) -- re-subscribing to the same city
-- just refreshes contact/radius (see the upsert below) instead of piling
-- up duplicate rows that'd all match and queue separately.
CREATE UNIQUE INDEX IF NOT EXISTS idx_city_subscribers_owner_city
    ON city_subscribers (owner, geoname_id);
-- NULL (the default) keeps the original "exact nearest-city" match; set
-- to switch this subscription to distance-from-city-center matching
-- instead -- see notify.py's queue_notifications.
ALTER TABLE city_subscribers ADD COLUMN IF NOT EXISTS radius_miles DOUBLE PRECISION;

-- Unifies subscribers (point + radius) and city_subscribers (whole city
-- or city + radius) into one read shape -- /manage's combined listing and
-- notification_log's owner-scoping join use this instead of querying both
-- tables and merging in Python. Re-created (not just CREATE IF NOT
-- EXISTS) so a rerun always reflects the latest column set here.
CREATE OR REPLACE VIEW subscriptions_view AS
SELECT
    s.id, 'point'::text AS kind, s.owner, s.contact,
    s.latitude, s.longitude, s.radius_miles,
    NULL::bigint AS geoname_id, NULL::text AS city_name, s.created_at
FROM subscribers s
UNION ALL
SELECT
    cs.id, 'city'::text AS kind, cs.owner, cs.contact,
    c.latitude, c.longitude, cs.radius_miles,
    cs.geoname_id, c.name AS city_name, cs.created_at
FROM city_subscribers cs
JOIN cities c ON c.geoname_id = cs.geoname_id;
"""

UPSERT = """
INSERT INTO city_subscribers (owner, contact, geoname_id, radius_miles)
VALUES (%(owner)s, %(contact)s, %(geoname_id)s, %(radius_miles)s)
ON CONFLICT (owner, geoname_id) DO UPDATE
    SET contact = EXCLUDED.contact, radius_miles = EXCLUDED.radius_miles
RETURNING id;
"""


def init_db() -> None:
    """Create the city_subscribers table and subscriptions_view.

    Depends on subscribers and cities already existing (the view joins
    both, and the FK needs cities) -- call subscribers.init_db() and
    cities.init_db() first.
    """
    logger.info("Ensuring city_subscribers table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def add_subscriber(
    owner: str,
    contact: str,
    geoname_id: int,
    radius_miles: float | None = None,
) -> int:
    """Register (or refresh) a subscription to `geoname_id`, returning its id.

    `owner` is the logged-in username that created it (see users.py) --
    only that user can cancel it later. `radius_miles` left as None (the
    default) matches only detections whose resolved nearest city is this
    one; set it to match by distance from the city's own point instead
    (see notify.py).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                UPSERT,
                {
                    "owner": owner,
                    "contact": contact,
                    "geoname_id": geoname_id,
                    "radius_miles": radius_miles,
                },
            )
            row = cur.fetchone()
        conn.commit()

    assert row is not None  # INSERT ... RETURNING id always returns a row
    logger.info(
        f"Added city subscriber {row[0]} (geoname_id={geoname_id}, "
        f"radius_miles={radius_miles}, owner={owner!r})"
    )
    return row[0]


def get_subscribers() -> list[dict[str, Any]]:
    """List every active city subscription, across every owner, with the
    city's own point joined in.

    Used internally for matching (see notify.py) -- not exposed over the
    API as-is, since that would leak every subscriber's contact info to
    every logged-in user. See `get_subscribers_by_owner` for the
    per-account view /city-subscribers/mine uses.

    The join (rather than a bare `SELECT *`) is what lets notify.py match
    a radius-based city subscription by distance from `latitude`/
    `longitude` without a second lookup per subscriber.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT cs.*, c.latitude, c.longitude, c.name AS city_name "
            "FROM city_subscribers cs JOIN cities c ON c.geoname_id = cs.geoname_id"
        )
        return cur.fetchall()


def get_subscribers_by_owner(owner: str) -> list[dict[str, Any]]:
    """List only the city subscriptions created by `owner`, with the city's
    name/admin1/country joined in so the UI doesn't need a second lookup
    per row.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT cs.id, cs.owner, cs.contact, cs.geoname_id, cs.radius_miles, cs.created_at, "
            "c.name AS city_name, c.admin1_code, c.country_code "
            "FROM city_subscribers cs JOIN cities c ON c.geoname_id = cs.geoname_id "
            "WHERE cs.owner = %(owner)s ORDER BY cs.created_at DESC",
            {"owner": owner},
        )
        return cur.fetchall()


def get_subscriber(subscriber_id: int, owner: str) -> dict[str, Any] | None:
    """Fetch one city subscription, scoped to `owner` the same way
    get_subscribers_by_owner is -- None if it doesn't exist or belongs to
    someone else.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM city_subscribers WHERE id = %(id)s AND owner = %(owner)s",
            {"id": subscriber_id, "owner": owner},
        )
        return cur.fetchone()


def set_contact(subscriber_id: int, owner: str, contact: str) -> bool:
    """Rename the label on a city subscription. Returns False if no
    matching subscription was owned by `owner` (either it doesn't exist or
    belongs to someone else).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE city_subscribers SET contact = %(contact)s "
                "WHERE id = %(id)s AND owner = %(owner)s",
                {"contact": contact, "id": subscriber_id, "owner": owner},
            )
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def set_radius(subscriber_id: int, owner: str, radius_miles: float | None) -> bool:
    """Set (or clear, with radius_miles=None) the match radius on a city
    subscription. Clearing it reverts to matching only the exact
    nearest-city case. Returns False if no matching subscription was owned
    by `owner` (either it doesn't exist or belongs to someone else).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE city_subscribers SET radius_miles = %(radius_miles)s "
                "WHERE id = %(id)s AND owner = %(owner)s",
                {"radius_miles": radius_miles, "id": subscriber_id, "owner": owner},
            )
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def remove_subscriber(subscriber_id: int, owner: str) -> bool:
    """Cancel a city subscription. Returns False if no matching subscription
    was owned by `owner` (either it doesn't exist or belongs to someone else).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM city_subscribers WHERE id = %(id)s AND owner = %(owner)s",
                {"id": subscriber_id, "owner": owner},
            )
            removed = cur.rowcount > 0
        conn.commit()
    return removed
