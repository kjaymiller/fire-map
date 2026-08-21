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

Two kinds of subscription are matched here: point + radius (subscribers.py)
and whole-city (city_subscribers.py) -- see queue_notifications for how each
is matched, and notification_log's `subscription_kind` column for how the
two are told apart downstream (their ids come from separate tables, so
`subscriber_id` alone doesn't disambiguate).
"""

import json
import logging
import uuid
from collections.abc import Iterable
from typing import Any, cast

import valkey.exceptions
from geojson import Feature

from . import (
    NOTIFY_KEY_PREFIX,
    NOTIFY_SCAN_QUEUE_KEY,
    city_history,
    city_subscribers,
    notification_log,
    subscribers,
)
from .cache import event_id, get_client
from .cities import nearest_city
from .geo import distance_miles

logger = logging.getLogger(__name__)


def queue_notifications(
    features: Iterable[Feature],
    scan_id: uuid.UUID | str,
) -> int:
    """Match this scan's detections against subscribers and queue one
    notification per (subscriber, detection) pair within the subscriber's
    radius. Returns the number queued.
    """
    features = list(features)
    point_subs = subscribers.get_subscribers()
    city_subs = city_subscribers.get_subscribers()
    if not point_subs and not city_subs:
        logger.info(f"No subscribers registered, nothing to match for scan {scan_id}")
        return 0

    matches: list[dict[str, Any]] = []

    for feature in features:
        lon, lat = feature["geometry"]["coordinates"]
        # Same detection identity cache.py's snapshot keys use -- computed
        # once per feature (not per subscriber match) since it only
        # depends on the detection itself.
        detection_key = event_id(feature)

        # Resolved once per detection regardless of whether it matches any
        # subscriber -- unlike the old subscriber-gated lookup this
        # replaced, city_fire_history is meant to be a *full* history of
        # what's landed near a city, not just what happened to trigger an
        # alert for someone.
        town: dict[str, Any] | None = None
        try:
            town = nearest_city(lat, lon)
        except Exception:
            # cities table might not exist/be loaded yet -- a missing town
            # label shouldn't stop the alert itself.
            logger.exception(f"Nearest-city lookup failed for {lat}, {lon}")

        if town is not None:
            try:
                city_history.record_detection(
                    town, feature, scan_id, town["distance_miles"], detection_key
                )
            except Exception:
                logger.exception(
                    f"Failed to record detection near {town['name']!r} in city history"
                )

        for sub in point_subs:
            distance = distance_miles(lat, lon, sub["latitude"], sub["longitude"])
            if distance > sub["radius_miles"]:
                continue

            matches.append(
                {
                    "subscription_kind": "point",
                    "subscriber_id": sub["id"],
                    "owner": sub["owner"],
                    "contact": sub["contact"],
                    "distance_miles": round(distance, 1),
                    "detected_at": feature["properties"]["datetime"],
                    "latitude": lat,
                    "longitude": lon,
                    "feature": feature,
                    "detection_key": detection_key,
                    "town": town,
                    # The alert area's own center/radius -- included so a
                    # batched message covering several of an owner's areas
                    # (see notifier.py:build_batch_message) can label which
                    # area each detection matched, not just its distance.
                    "sub_latitude": sub["latitude"],
                    "sub_longitude": sub["longitude"],
                    "sub_radius_miles": sub["radius_miles"],
                }
            )

        # A city subscription with no radius_miles set matches only the
        # exact nearest-city case (same as what /towns/{geoname_id}/history
        # shows); one with a radius matches by distance from the city's own
        # point instead, same as a point subscription, so it isn't limited
        # to detections whose nearest city happens to resolve to this one.
        for sub in city_subs:
            if sub["radius_miles"] is not None:
                distance = distance_miles(lat, lon, sub["latitude"], sub["longitude"])
                if distance > sub["radius_miles"]:
                    continue
            else:
                if town is None or sub["geoname_id"] != town["geoname_id"]:
                    continue
                distance = town["distance_miles"]

            matches.append(
                {
                    "subscription_kind": "city",
                    "subscriber_id": sub["id"],
                    "owner": sub["owner"],
                    "contact": sub["contact"],
                    "distance_miles": round(distance, 1),
                    "detected_at": feature["properties"]["datetime"],
                    "latitude": lat,
                    "longitude": lon,
                    "feature": feature,
                    "detection_key": detection_key,
                    # The detection's own nearest-city lookup -- used for
                    # the per-item "near X" line. Kept separate from
                    # city_name/city_admin1_code/city_country_code below
                    # (the *subscribed* city), since a radius-based city
                    # subscription can match a detection whose own nearest
                    # town resolves to somewhere else nearby, or nowhere at
                    # all (town is None) -- see notifier.py's area_header.
                    "town": town,
                    "city_name": sub["city_name"],
                    "city_admin1_code": sub.get("city_admin1_code"),
                    "city_country_code": sub.get("city_country_code"),
                }
            )

    if not matches:
        logger.info(
            f"No subscriber matches for scan {scan_id} "
            f"({len(features)} detection(s), {len(point_subs)} point + "
            f"{len(city_subs)} city subscriber(s))"
        )
        return 0

    # Durably logged before the ephemeral Valkey queue -- each row_id rides
    # along in its match's payload (as event_id) so the notifier can update
    # this same row once it knows whether the send actually went out. A
    # match whose (subscription_kind, subscriber_id, detection_key) was
    # already logged in some earlier scan comes back None here -- that's
    # the dedup check, and it's dropped entirely rather than queued again
    # (see log_queued).
    row_ids = notification_log.log_queued(scan_id, matches)
    skipped = sum(1 for row_id in row_ids if row_id is None)

    pending: dict[str, str] = {}
    for match, row_id in zip(matches, row_ids, strict=True):
        if row_id is None:
            continue
        field = (
            f"notification:{match['subscription_kind']}:{match['subscriber_id']}"
            f":{match['detected_at']}"
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
