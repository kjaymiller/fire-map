"""Location-based fire alerts.

Every reload, freshly fetched detections are matched against every
subscriber's point + radius. Each hit is queued as one field in a
per-scan Valkey hash (`firemap:notify:<scan_id>`) so a worker can claim a
batch of pending sends in one atomic HGETDEL call -- claimed fields are
retrieved and removed together, so a notification is never sent twice, and
a crash mid-send only costs the notifications actually in flight.

Scan ids with anything queued are also pushed onto a shared list
(NOTIFY_SCAN_QUEUE_KEY) so the notifier service (src/notifier.py) can block
on new work with BLPOP instead of polling scan ids it has to guess at.

Each match is also durably logged to Postgres (notification_log.py) at
queue time, since the Valkey queue itself is deleted the moment it's
claimed -- that table is what /manage's trigger history reads from, and
also what dedup checks against: FIRMS' rolling window can keep handing back
the same physical detection across several consecutive reloads before it
ages out, and a subscriber should hear about it once, not once per reload
it happens to still be in that window.
"""

import json
import logging
import math
import uuid
from collections.abc import Iterable
from typing import Any, cast

import valkey.exceptions
from geojson import Feature

from . import NOTIFY_KEY_PREFIX, NOTIFY_SCAN_QUEUE_KEY, notification_log, subscribers
from .cache import event_id, get_client

logger = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.8


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in miles (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def queue_notifications(
    features: Iterable[Feature],
    scan_id: uuid.UUID | str,
) -> int:
    """Match this scan's detections against subscribers and queue one
    notification per (subscriber, detection) pair within the subscriber's
    radius. Returns the number queued.
    """
    features = list(features)
    subs = subscribers.get_subscribers()
    if not subs:
        logger.info(f"No subscribers registered, nothing to match for scan {scan_id}")
        return 0

    matches: list[dict[str, Any]] = []

    for feature in features:
        lon, lat = feature["geometry"]["coordinates"]
        # Same detection identity cache.py's snapshot keys use -- computed
        # once per feature (not per subscriber match) since it only
        # depends on the detection itself.
        detection_key = event_id(feature)
        for sub in subs:
            distance = distance_miles(lat, lon, sub["latitude"], sub["longitude"])
            if distance > sub["radius_miles"]:
                continue

            matches.append(
                {
                    "subscriber_id": sub["id"],
                    "owner": sub["owner"],
                    "contact": sub["contact"],
                    "distance_miles": round(distance, 1),
                    "detected_at": feature["properties"]["datetime"],
                    "latitude": lat,
                    "longitude": lon,
                    "feature": feature,
                    "detection_key": detection_key,
                    # The alert area's own center/radius -- included so a
                    # batched message covering several of an owner's areas
                    # (see notifier.py:build_batch_message) can label which
                    # area each detection matched, not just its distance.
                    "sub_latitude": sub["latitude"],
                    "sub_longitude": sub["longitude"],
                    "sub_radius_miles": sub["radius_miles"],
                }
            )

    if not matches:
        logger.info(
            f"No subscriber matches for scan {scan_id} "
            f"({len(features)} detection(s), {len(subs)} subscriber(s))"
        )
        return 0

    # Durably logged before the ephemeral Valkey queue -- each row_id rides
    # along in its match's payload (as event_id) so the notifier can update
    # this same row once it knows whether the send actually went out. A
    # match whose (subscriber_id, detection_key) was already logged in some
    # earlier scan comes back None here -- that's the dedup check, and it's
    # dropped entirely rather than queued again (see log_queued).
    row_ids = notification_log.log_queued(scan_id, matches)
    skipped = sum(1 for row_id in row_ids if row_id is None)

    pending: dict[str, str] = {}
    for match, row_id in zip(matches, row_ids, strict=True):
        if row_id is None:
            continue
        field = (
            f"notification:{match['subscriber_id']}:{match['detected_at']}"
            f":{match['latitude']:.4f}:{match['longitude']:.4f}"
        )
        pending[field] = json.dumps({**match, "event_id": row_id})

    if skipped:
        logger.info(f"Skipped {skipped} already-notified detection(s) for scan {scan_id}")

    if not pending:
        logger.info(f"All {len(matches)} match(es) for scan {scan_id} were already notified")
        return 0

    hash_key = f"{NOTIFY_KEY_PREFIX}{scan_id}"
    client = get_client()
    client.hset(hash_key, mapping=pending)
    # Announce this scan to the notifier -- see next_scan_id below.
    client.rpush(NOTIFY_SCAN_QUEUE_KEY, str(scan_id))
    logger.info(f"Queued {len(pending)} notifications for scan {scan_id}")
    return len(pending)


def next_scan_id(timeout: int = 5) -> str | None:
    """Block up to `timeout` seconds for the next scan_id with pending
    notifications, or return None if nothing showed up in time.

    Used by the notifier service to avoid busy-polling scan_ids it has no
    other way to discover.
    """
    client = get_client()
    try:
        result = cast(
            "tuple[str, str] | None",
            client.blpop([NOTIFY_SCAN_QUEUE_KEY], timeout=timeout),
        )
    except valkey.exceptions.TimeoutError:
        # The client's own socket read timeout (set when the connection was
        # created, unrelated to the BLPOP `timeout` above) can fire first
        # if it's <= our block timeout -- that's just "nothing showed up",
        # not a real error.
        return None
    return result[1] if result is not None else None


def requeue_scan_id(scan_id: uuid.UUID | str) -> None:
    """Push a scan_id back onto the queue -- used when a claim only drained
    part of its hash, so the next iteration comes back for the rest.
    """
    get_client().rpush(NOTIFY_SCAN_QUEUE_KEY, str(scan_id))


def pending_count(scan_id: uuid.UUID | str) -> int:
    """How many notifications are still queued for this scan."""
    return cast(int, get_client().hlen(f"{NOTIFY_KEY_PREFIX}{scan_id}"))


def claim_notifications(scan_id: uuid.UUID | str, limit: int = 50) -> list[dict[str, Any]]:
    """Atomically claim (retrieve + remove) up to `limit` pending
    notifications for a scan. Anything left in the hash stays queued for
    the next worker to claim.
    """
    client = get_client()
    hash_key = f"{NOTIFY_KEY_PREFIX}{scan_id}"
    # See the cast note in cache.py:get_current -- valkey's stubs type
    # these as `Awaitable[Any] | Any` to cover the async client too.
    fields = cast("list[str]", client.hkeys(hash_key))[:limit]

    if not fields:
        return []

    raw = cast(
        "list[str | None]",
        client.execute_command("HGETDEL", hash_key, "FIELDS", len(fields), *fields),
    )
    return [json.loads(value) for value in raw if value is not None]
