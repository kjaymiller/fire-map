"""Notification worker: drains queued location alerts and sends them via
Apprise (https://apprise.kjaymiller.dev/).

Runs as its own docker-compose service (like scheduler), talking to
Postgres/Valkey directly rather than through the web app's HTTP API. The
loop is intentionally simple and stateless -- claim a batch, send each
recipient's channel one message, move on. A batch that fails to send is
logged and dropped; there's no retry queue, matching
notify.claim_notifications' own guarantee (a notification is claimed at
most once, not delivered at most once).

    scan queued (notify.queue_notifications)
        -> pushed onto NOTIFY_SCAN_QUEUE_KEY
    notifier: next_scan_id() [[BLPOP]]
        -> claim_notifications(scan_id) [[HGETDEL]]
        -> group_by_channel(): one owner's channels looked up once, every
           matching detection folded under its (channel_type, address)
        -> one Apprise/API call per (channel_type, address), not per
           detection -- Apprise's own Mailgun batching sends identical
           content to everyone in a call, which doesn't fit our
           per-recipient distance/detail text, so this is where the actual
           call-count reduction comes from instead.
        -> requeue_scan_id() if the hash wasn't fully drained
"""

import html
import logging
from datetime import UTC, datetime
from typing import Any

import apprise

from src import otel

from .db import channels, notification_log, notify

logger = logging.getLogger(__name__)

# Drain everything queued for a scan in one pass, up to Mailgun's per-call
# recipient cap -- also just a sane upper bound so one scan can't hold the
# worker forever if something goes very wrong upstream.
CLAIM_BATCH_SIZE = 1000
# How long to block waiting for the next scan before looping again (lets
# the process notice a shutdown signal instead of blocking forever).
POLL_TIMEOUT_SECONDS = 5


def format_detected_at(iso_string: str) -> str:
    """FIRMS timestamps are naive ISO strings but actually UTC -- render
    something readable instead of the raw '2026-08-11T08:39:00'.
    """
    try:
        parsed = datetime.fromisoformat(iso_string).replace(tzinfo=UTC)
    except ValueError:
        return iso_string
    return parsed.strftime("%b %d, %I:%M %p UTC")


def maps_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps?q={lat:.4f},{lon:.4f}"


def town_label(notification: dict[str, Any]) -> str | None:
    """'Springfield, IL, US' for a notification with a resolved nearest
    town (see notify.queue_notifications), or None if the detection was too
    far from anything in the cities table to label (see cities.nearest_city).
    """
    town = notification.get("town")
    if town is None:
        return None
    parts = [town["name"]]
    parts.extend(part for part in (town.get("admin1_code"), town.get("country_code")) if part)
    return ", ".join(parts)


def group_by_area(
    notifications: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split one recipient's notifications by alert area, each sorted by
    distance -- an owner with more than one area would otherwise see one
    flattened list mixing distances relative to different centers (0.2 mi
    from one area, 90 mi from another, with no indication which area either
    number is relative to).

    Keyed on (subscription_kind, subscriber_id), not subscriber_id alone --
    point and city subscriptions each have their own id sequence starting
    at 1, so a bare id could collide across kinds.
    """
    by_area: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for notification in notifications:
        key = (notification["subscription_kind"], notification["subscriber_id"])
        by_area.setdefault(key, []).append(notification)

    groups = list(by_area.values())
    for group in groups:
        group.sort(key=lambda n: n["distance_miles"])
    return groups


def area_header(area: dict[str, Any]) -> str:
    """'Within 25 mi of 33.7, -84.4' for a point subscription, or 'Near
    Springfield, IL, US' for a city one -- the label at the top of each
    group in a batched alert (see render_html/render_markdown).

    Built from the *subscribed* city's own name (city_name/
    city_admin1_code/city_country_code -- see notify.py's queue_notifications),
    not town_label()'s per-detection nearest-city lookup: a radius-based
    city subscription can match a detection whose own nearest town
    resolves to somewhere else nearby, or to nothing at all, which used to
    make this header fall back to a generic "your subscribed city" instead
    of naming the city that was actually subscribed to.
    """
    if area["subscription_kind"] == "city":
        parts = [area["city_name"]]
        parts.extend(
            part for part in (area.get("city_admin1_code"), area.get("city_country_code")) if part
        )
        return f"Near {', '.join(parts)}"
    return (
        f"Within {area['sub_radius_miles']:g} mi of "
        f"{area['sub_latitude']:.4f}, {area['sub_longitude']:.4f}"
    )


def build_title(notifications: list[dict[str, Any]]) -> str:
    count = len(notifications)
    return (
        "Fire detected near your alert area"
        if count == 1
        else f"{count} fires detected near your alert areas"
    )


def render_html(notifications: list[dict[str, Any]]) -> str:
    """Render for channels that display real HTML (email)."""
    sections = []
    for group in group_by_area(notifications):
        area = group[0]
        header = f"<p><strong>{html.escape(area_header(area))}</strong></p>"

        items = []
        for notification in group:
            feature = notification["feature"]
            props = feature["properties"]
            lon, lat = feature["geometry"]["coordinates"]
            satellite = html.escape(str(props.get("satellite", "unknown")))
            town = town_label(notification)
            near = f"near {html.escape(town)} " if town else ""
            items.append(
                "<li>"
                f"<strong>{notification['distance_miles']} mi away</strong> &mdash; "
                f'{near}<a href="{maps_link(lat, lon)}">{lat:.4f}, {lon:.4f}</a>, '
                f"detected {format_detected_at(props.get('datetime', ''))} "
                f"(satellite: {satellite})"
                "</li>"
            )
        sections.append(header + "<ul>" + "".join(items) + "</ul>")

    return (
        "<div>"
        + "".join(sections)
        + '<p style="color:#606c76;font-size:0.9em">This is not an emergency tool -- '
        + "detections are from delayed satellite passes, not real-time conditions.</p>"
        + "</div>"
    )


def render_markdown(notifications: list[dict[str, Any]]) -> str:
    """Render for channels that speak their own chat markdown, not HTML
    (Discord dumps raw HTML tags into the message as literal text --
    Apprise's Discord plugin doesn't convert them, see notifier.py's
    send_channel_batch).
    """
    sections = []
    for group in group_by_area(notifications):
        area = group[0]
        lines = [f"**{area_header(area)}**"]
        for notification in group:
            feature = notification["feature"]
            props = feature["properties"]
            lon, lat = feature["geometry"]["coordinates"]
            town = town_label(notification)
            near = f"near {town} " if town else ""
            lines.append(
                f"- **{notification['distance_miles']} mi away** — "
                f"{near}[{lat:.4f}, {lon:.4f}]({maps_link(lat, lon)}), "
                f"detected {format_detected_at(props.get('datetime', ''))} "
                f"(satellite: {props.get('satellite', 'unknown')})"
            )
        sections.append("\n".join(lines))

    sections.append("_This is not an emergency tool -- detections are delayed satellite data._")
    return "\n\n".join(sections)


# Discord speaks chat markdown, not HTML -- everything else defaults to the
# HTML renderer (fine for the only other deliverable type today, email; see
# channels.DELIVERABLE_CHANNEL_TYPES).
_MARKDOWN_CHANNEL_TYPES = ("discord",)


def build_batch_message(
    channel_type: str, notifications: list[dict[str, Any]]
) -> tuple[str, str, str]:
    """Render a (title, body, apprise_format) covering every detection
    matched to one recipient's channel in this drain, in whatever format
    that channel type actually renders.
    """
    title = build_title(notifications)
    if channel_type in _MARKDOWN_CHANNEL_TYPES:
        return title, render_markdown(notifications), apprise.NotifyFormat.MARKDOWN
    return title, render_html(notifications), apprise.NotifyFormat.HTML


def group_by_channel(
    batch: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group a claimed batch by (channel_type, address) so each recipient's
    channel gets exactly one message per scan covering every detection that
    matched them, no matter how many separate (subscriber, detection) pairs
    queued for them individually.

    Channels are looked up once per owner (not once per notification) --
    a batch commonly holds several notifications for the same owner.
    """
    channels_by_owner: dict[str, list[dict[str, Any]]] = {}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for notification in batch:
        owner = notification["owner"]
        if owner not in channels_by_owner:
            channels_by_owner[owner] = channels.get_channels_by_owner(owner)

        owner_channels = channels_by_owner[owner]
        if not owner_channels:
            logger.info(f"No channels registered for owner {owner!r}, dropping notification")
            continue

        for channel in owner_channels:
            if channel["channel_type"] not in channels.DELIVERABLE_CHANNEL_TYPES:
                continue
            key = (channel["channel_type"], channel["address"])
            grouped.setdefault(key, []).append(notification)

    return grouped


def send_channel_batch(
    channel_type: str, address: str, notifications: list[dict[str, Any]]
) -> bool:
    """Send one message covering every notification grouped under this
    (channel_type, address) -- one Apprise call per recipient's channel per
    scan, instead of one per detection.
    """
    url = channels.to_apprise_url(channel_type, address)
    if url is None:
        return False

    title, body, body_format = build_batch_message(channel_type, notifications)
    apobj = apprise.Apprise()
    apobj.add(url)
    return bool(apobj.notify(title=title, body=body, body_format=body_format))


def drain_scan(scan_id: str) -> None:
    """Claim everything queued for `scan_id` (up to CLAIM_BATCH_SIZE), group
    it by recipient channel, and send one message per channel -- instead of
    one message per detection. Every claimed notification's durable
    notification_events row (see notify.queue_notifications) gets updated
    with the outcome -- delivered, failed, or no_channels -- so /manage's
    trigger history reflects what actually happened.

    claim_notifications' HGETDEL has already removed the batch from Valkey
    by the time any of the bookkeeping below runs, so a failure past that
    point can't be retried by picking the scan back up -- everything after
    the claim is therefore its own try/except, logged and skipped rather
    than left to propagate. That keeps one scan's bookkeeping failure from
    also taking down `run`'s loop and, with it, every *other* scan still
    waiting to be claimed.
    """
    try:
        batch = notify.claim_notifications(scan_id, limit=CLAIM_BATCH_SIZE)
    except Exception:
        logger.exception(f"Failed to claim notifications for scan {scan_id}")
        return

    if not batch:
        return

    logger.info(f"Claimed {len(batch)} notification(s) for scan {scan_id}")
    grouped = group_by_channel(batch)

    attempted_ids: set[int] = set()
    sent = 0
    for (channel_type, address), notifications in grouped.items():
        event_ids = [n["event_id"] for n in notifications]
        attempted_ids.update(event_ids)
        try:
            if send_channel_batch(channel_type, address, notifications):
                sent += 1
                notification_log.mark_delivered(event_ids, channel_type)
            else:
                logger.warning(
                    f"Failed to deliver {channel_type} batch of {len(notifications)} "
                    f"notification(s) to {address!r}"
                )
                notification_log.mark_failed(event_ids)
        except Exception:  # one bad channel shouldn't kill the worker
            logger.exception(f"Failed to send {channel_type} batch to {address!r}")
            notification_log.mark_failed(event_ids)

    unattempted_ids = [n["event_id"] for n in batch if n["event_id"] not in attempted_ids]
    try:
        notification_log.mark_no_channels(unattempted_ids)
    except Exception:
        logger.exception(f"Failed to mark no-channel notification(s) for scan {scan_id}")

    logger.info(
        f"Sent {sent}/{len(grouped)} channel batch(es) for scan {scan_id} "
        f"({len(batch)} notification(s) total)"
    )

    try:
        if notify.pending_count(scan_id) > 0:
            notify.requeue_scan_id(scan_id)
    except Exception:
        logger.exception(f"Failed to check/requeue remaining notifications for scan {scan_id}")


def run() -> None:
    logger.info("Notifier started, waiting for queued scans")
    while True:
        scan_id = notify.next_scan_id(timeout=POLL_TIMEOUT_SECONDS)
        if scan_id is None:
            continue
        try:
            drain_scan(scan_id)
        except Exception:  # a scan-level failure shouldn't kill the worker either
            logger.exception(f"Failed to drain scan {scan_id}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    otel.setup_telemetry(service_name="fire-map-notifier")
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Notifier shutting down")
