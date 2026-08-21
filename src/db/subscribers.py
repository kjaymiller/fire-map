"""Postgres storage for location-based fire alert subscriptions.

Each subscriber registers a point and a radius; every reload, `notify.py`
matches freshly fetched detections against this table to decide who gets
alerted. This is durable (unlike the Valkey-backed notification queue in
notify.py), so a subscription survives cache expiry and restarts.

A subscription created with `ephemeral=True` (the map's "notify me about
this area" flow, as opposed to the durable form on /notify) is meant to
track a fire while it's active rather than to sit around forever: see
`expire_ephemeral` below, called once per reload from update.reload_data
with that reload's live detections, which deletes any ephemeral
subscription whose radius no longer contains a currently-detected point.
Non-ephemeral subscriptions are never touched by that -- they persist
until a user deletes them, same as always. Either way, the detection
history itself (Postgres' fire_detections table, notification_log) is
untouched by expiry; only the subscription row goes away.
"""

import logging
from collections.abc import Iterable
from typing import Any

from psycopg.rows import dict_row

from .geo import distance_miles
from .postgres import get_pool

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id BIGSERIAL PRIMARY KEY,
    contact TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    radius_miles DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Added when subscriptions moved behind login -- ADD COLUMN IF NOT EXISTS
-- so this is safe to rerun against a table created before that change.
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'legacy';
-- Marks a subscription as tied to an active fire rather than durable --
-- see expire_ephemeral below and the module docstring above.
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS ephemeral BOOLEAN NOT NULL DEFAULT false;
"""

INSERT = """
INSERT INTO subscribers (owner, contact, latitude, longitude, radius_miles, ephemeral)
VALUES (%(owner)s, %(contact)s, %(latitude)s, %(longitude)s, %(radius_miles)s, %(ephemeral)s)
RETURNING id;
"""


def init_db() -> None:
    """Create the subscribers table if it doesn't already exist."""
    logger.info("Ensuring subscribers table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def add_subscriber(
    owner: str,
    contact: str,
    latitude: float,
    longitude: float,
    radius_miles: float,
    ephemeral: bool = False,
) -> int:
    """Register a subscription, returning its new id.

    `owner` is the logged-in username that created it (see users.py) --
    only that user can cancel it later. `ephemeral=True` marks this as
    tied to an active fire rather than durable -- see expire_ephemeral.
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                INSERT,
                {
                    "owner": owner,
                    "contact": contact,
                    "latitude": latitude,
                    "longitude": longitude,
                    "radius_miles": radius_miles,
                    "ephemeral": ephemeral,
                },
            )
            row = cur.fetchone()
        conn.commit()

    assert row is not None  # INSERT ... RETURNING id always returns a row
    logger.info(
        f"Added subscriber {row[0]} ({radius_miles} mi radius, owner={owner!r}, "
        f"ephemeral={ephemeral})"
    )
    return row[0]


def get_subscribers() -> list[dict[str, Any]]:
    """List every active subscription, across every owner.

    Used internally for matching (see notify.py) -- not exposed over the
    API as-is, since that would leak every subscriber's contact info to
    every logged-in user. See `get_subscribers_by_owner` for the
    per-account view the /subscribers/mine route uses.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM subscribers")
        return cur.fetchall()


def get_subscribers_by_owner(owner: str) -> list[dict[str, Any]]:
    """List only the subscriptions created by `owner` -- the account a
    logged-in user is allowed to see and manage.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM subscribers WHERE owner = %(owner)s ORDER BY created_at DESC",
            {"owner": owner},
        )
        return cur.fetchall()


def get_subscriber(subscriber_id: int, owner: str) -> dict[str, Any] | None:
    """Fetch one subscription, scoped to `owner` the same way
    get_subscribers_by_owner is -- None if it doesn't exist or belongs to
    someone else.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM subscribers WHERE id = %(id)s AND owner = %(owner)s",
            {"id": subscriber_id, "owner": owner},
        )
        return cur.fetchone()


def set_contact(subscriber_id: int, owner: str, contact: str) -> bool:
    """Rename the label on a subscription. Returns False if no matching
    subscription was owned by `owner` (either it doesn't exist or belongs
    to someone else).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET contact = %(contact)s "
                "WHERE id = %(id)s AND owner = %(owner)s",
                {"contact": contact, "id": subscriber_id, "owner": owner},
            )
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def remove_subscriber(subscriber_id: int, owner: str) -> bool:
    """Cancel a subscription. Returns False if no matching subscription was
    owned by `owner` (either it doesn't exist or belongs to someone else).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM subscribers WHERE id = %(id)s AND owner = %(owner)s",
                {"id": subscriber_id, "owner": owner},
            )
            removed = cur.rowcount > 0
        conn.commit()
    return removed


def expire_ephemeral(active_points: Iterable[tuple[float, float]]) -> list[int]:
    """Delete every ephemeral subscription whose radius no longer contains
    any currently-live detection.

    `active_points` is this reload's fresh (latitude, longitude) pairs --
    see update.reload_data, which calls this right after queue_notifications
    with the same detections notify.py just matched. A non-ephemeral
    subscription is never a candidate here regardless of what's nearby;
    only ones created with ephemeral=True (see add_subscriber) are. Nothing
    else about the fire is touched -- fire_detections and notification_log
    keep their full history either way, so "expires" here means only the
    subscription row, not the record of what it once matched.

    Returns the ids removed.
    """
    active_points = list(active_points)
    subs = get_subscribers()
    expired_ids = [
        sub["id"]
        for sub in subs
        if sub["ephemeral"]
        and not any(
            distance_miles(lat, lon, sub["latitude"], sub["longitude"]) <= sub["radius_miles"]
            for lat, lon in active_points
        )
    ]
    if not expired_ids:
        return []

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM subscribers WHERE id = ANY(%(ids)s)",
                {"ids": expired_ids},
            )
        conn.commit()

    logger.info(f"Expired {len(expired_ids)} ephemeral subscription(s) with no fire left nearby")
    return expired_ids
