"""Durable record of location-alert triggers.

notify.py's Valkey queue is deliberately ephemeral -- a claimed notification
is gone the moment it's claimed (see notify.claim_notifications' HGETDEL),
so there's no way to look back at what fired for a given subscription once
the notifier's drained it. This table is that history: one row per
(subscriber, detection) match, written when it's queued and updated once
the notifier knows whether it actually went out -- see notify.py's
queue_notifications and notifier.py's drain_scan.

It also doubles as the dedup log: FIRMS' rolling window can return the same
physical detection (same point, same acquisition time, same satellite --
see cache.py's event_id) across several consecutive reloads before it ages
out. `log_queued`'s ON CONFLICT DO NOTHING against (subscription_kind,
subscriber_id, detection_key) means a detection only ever queues a
notification once per subscription, no matter how many more times FIRMS
keeps handing it back.

`subscriber_id` alone isn't a stable reference -- notify.py matches two
kinds of subscription (point + radius, in subscribers.py, and whole-city,
in city_subscribers.py) that each have their own id sequence starting at 1,
so `subscription_kind` disambiguates which table an id came from. That
also means there's no DB-level FK on subscriber_id here (it can point to
either table); ownership is instead checked by joining subscriptions_view
(see city_subscribers.py), which already knows how to route by kind.
"""

import logging
import uuid
from typing import Any

from psycopg.rows import dict_row

from .postgres import get_pool

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_events (
    id BIGSERIAL PRIMARY KEY,
    subscriber_id BIGINT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    scan_id UUID NOT NULL,
    distance_miles DOUBLE PRECISION NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    channels_delivered TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notification_events_subscriber
    ON notification_events (subscriber_id, created_at DESC);
-- Added when dedup moved here from being implicit in the Valkey queue --
-- ADD COLUMN IF NOT EXISTS so this is safe to rerun against a table
-- created before that change. Existing rows get '' (excluded from the
-- unique index below, via its WHERE clause) since there's nothing
-- meaningful to backfill them with.
ALTER TABLE notification_events ADD COLUMN IF NOT EXISTS detection_key TEXT NOT NULL DEFAULT '';
-- Added alongside city subscriptions -- subscriber_id's id sequence is
-- shared between two different tables now (subscribers, city_subscribers),
-- so this says which one. Existing rows predate city subscriptions and are
-- backfilled as 'point'. The FK above only ever pointed at subscribers, so
-- it's dropped here rather than trying to make it conditional on kind.
ALTER TABLE notification_events ADD COLUMN IF NOT EXISTS subscription_kind TEXT
    NOT NULL DEFAULT 'point';
ALTER TABLE notification_events DROP CONSTRAINT IF EXISTS notification_events_subscriber_id_fkey;
DROP INDEX IF EXISTS idx_notification_events_dedup;
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_events_dedup
    ON notification_events (subscription_kind, subscriber_id, detection_key)
    WHERE detection_key <> '';
"""

INSERT = """
INSERT INTO notification_events (
    subscriber_id, subscription_kind, scan_id, distance_miles, detected_at,
    latitude, longitude, detection_key
) VALUES (
    %(subscriber_id)s, %(subscription_kind)s, %(scan_id)s, %(distance_miles)s,
    %(detected_at)s, %(latitude)s, %(longitude)s, %(detection_key)s
)
ON CONFLICT (subscription_kind, subscriber_id, detection_key) WHERE detection_key <> '' DO NOTHING
RETURNING id;
"""


def init_db() -> None:
    """Create the notification_events table if it doesn't already exist.

    Depends on the subscribers table already existing (FK) -- call
    subscribers.init_db() first.
    """
    logger.info("Ensuring notification_events table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def log_queued(scan_id: uuid.UUID | str, matches: list[dict[str, Any]]) -> list[int | None]:
    """Record one row per queued match, in the same order as `matches` --
    except a match whose (subscription_kind, subscriber_id, detection_key)
    has already been logged before (any prior scan, not just this one),
    which logs nothing and comes back as None in that position instead.

    Each match needs subscription_kind, subscriber_id, distance_miles,
    detected_at, latitude, longitude, detection_key. The caller drops any
    None-paired match entirely rather than queuing it to Valkey -- that's
    the actual dedup enforcement point, this just detects it. Non-None ids
    get stitched back into their Valkey payload (as `event_id`) so the
    notifier can update the right row later.
    """
    if not matches:
        return []

    rows = [
        {
            "subscriber_id": match["subscriber_id"],
            "subscription_kind": match["subscription_kind"],
            "scan_id": str(scan_id),
            "distance_miles": match["distance_miles"],
            "detected_at": match["detected_at"],
            "latitude": match["latitude"],
            "longitude": match["longitude"],
            "detection_key": match["detection_key"],
        }
        for match in matches
    ]

    ids: list[int | None] = []
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                result = cur.execute(INSERT, row).fetchone()
                ids.append(result[0] if result is not None else None)
        conn.commit()

    return ids


def mark_delivered(event_ids: list[int], channel_type: str) -> None:
    """Record a successful send to `channel_type` for these events.

    Unconditionally wins over mark_failed -- if any channel succeeds, the
    event is "delivered" no matter what other channels did or what order
    the notifier processed them in.
    """
    if not event_ids:
        return

    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE notification_events SET status = 'delivered', "
            "channels_delivered = array_append(channels_delivered, %(channel_type)s) "
            "WHERE id = ANY(%(ids)s)",
            {"channel_type": channel_type, "ids": event_ids},
        )
        conn.commit()


def mark_failed(event_ids: list[int]) -> None:
    """Record that every channel attempted for these events failed.

    Guarded so it can never downgrade an event mark_delivered already
    marked delivered, regardless of processing order.
    """
    if not event_ids:
        return

    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE notification_events SET status = 'failed' "
            "WHERE id = ANY(%(ids)s) AND status != 'delivered'",
            {"ids": event_ids},
        )
        conn.commit()


def mark_no_channels(event_ids: list[int]) -> None:
    """Record that the owner had no deliverable channel at all -- nothing
    was even attempted for these events.
    """
    if not event_ids:
        return

    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE notification_events SET status = 'no_channels' WHERE id = ANY(%(ids)s)",
            {"ids": event_ids},
        )
        conn.commit()


def get_events_by_owner(owner: str, limit: int = 200) -> list[dict[str, Any]]:
    """List recent trigger events for every subscription `owner` has --
    point or city alike -- newest first. The history behind /manage's "how
    have my areas triggered" view.

    Joined against subscriptions_view (see city_subscribers.py) rather than
    subscribers directly, since subscriber_id's id sequence is shared
    between two tables now -- the join has to match on (kind, id) together,
    which subscriptions_view already knows how to do.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT ne.id, ne.subscriber_id, ne.subscription_kind, ne.scan_id, "
            "ne.distance_miles, ne.detected_at, ne.latitude, ne.longitude, ne.status, "
            "ne.channels_delivered, ne.created_at "
            "FROM notification_events ne "
            "JOIN subscriptions_view sv "
            "ON sv.kind = ne.subscription_kind AND sv.id = ne.subscriber_id "
            "WHERE sv.owner = %(owner)s "
            "ORDER BY ne.created_at DESC "
            "LIMIT %(limit)s",
            {"owner": owner, "limit": limit},
        )
        return cur.fetchall()
