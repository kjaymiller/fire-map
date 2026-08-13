"""Postgres storage for account-level notification channels.

Each user can register one or more delivery channels (email, SMS, Signal,
Telegram, Discord) under their account. This is intentionally separate from
`subscribers.py` -- a subscription is "alert me about fires near this
point"; a channel is "here's a way to reach me."

`to_apprise_url` turns a stored (channel_type, address) row into a URL
Apprise (https://apprise.kjaymiller.dev/) knows how to send through -- see
src/notifier.py for the worker that actually calls it.
"""

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row

from .postgres import get_pool

logger = logging.getLogger(__name__)

CHANNEL_TYPES = ("email", "sms", "signal", "telegram", "discord")

# Channel types actually wired to Apprise delivery today: email via Mailgun,
# discord via the account's own webhook URL. sms/signal/telegram each need a
# gateway or bot token this project doesn't stand up yet -- accounts can
# still register them, they just won't be sent to (see notifier.py, which
# skips and logs anything not in this set).
DELIVERABLE_CHANNEL_TYPES = ("email", "discord")

_DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/(?P<id>\d+)/(?P<token>[\w-]+)/?$"
)

# Shows up as the From-name in email and the bot name on Discord messages --
# without this, Apprise's own default ("Apprise") shows up instead, which
# doesn't mean anything to whoever's receiving the alert.
SENDER_NAME = os.environ.get("NOTIFY_SENDER_NAME", "Fire Map")


def to_apprise_url(channel_type: str, address: str) -> str | None:
    """Build an Apprise target URL for a stored channel, or None if this
    channel_type isn't wired to a delivery backend (see
    DELIVERABLE_CHANNEL_TYPES).

    email routes through one Mailgun account configured for the whole app
    (see NOTIFY_MAILGUN_* below); `address` is just the recipient. discord
    expects `address` to be the webhook URL Discord gives you when you
    create one.
    """
    if channel_type == "email":
        domain = os.environ.get("NOTIFY_MAILGUN_DOMAIN")
        api_key = os.environ.get("NOTIFY_MAILGUN_API_KEY")
        if not domain or not api_key:
            logger.warning(
                "NOTIFY_MAILGUN_DOMAIN/NOTIFY_MAILGUN_API_KEY not set -- "
                "can't deliver email channels"
            )
            return None
        from_user = os.environ.get("NOTIFY_MAILGUN_FROM_USER", "alerts")
        region = os.environ.get("NOTIFY_MAILGUN_REGION")  # "us" (default) or "eu"
        url = (
            f"mailgun://{quote(from_user)}@{domain}/{api_key}"
            f"/?to={quote(address)}&name={quote(SENDER_NAME)}"
        )
        if region:
            url += f"&region={region}"
        return url

    if channel_type == "discord":
        match = _DISCORD_WEBHOOK_RE.match(address.strip())
        if not match:
            logger.warning(f"Discord channel address isn't a webhook URL: {address!r}")
            return None
        return f"discord://{quote(SENDER_NAME)}@{match['id']}/{match['token']}"

    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_channels (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    address TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, channel_type, address)
);
"""

INSERT = """
INSERT INTO notification_channels (owner, channel_type, address)
VALUES (%(owner)s, %(channel_type)s, %(address)s)
RETURNING id;
"""


def init_db() -> None:
    """Create the notification_channels table if it doesn't already exist."""
    logger.info("Ensuring notification_channels table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def add_channel(owner: str, channel_type: str, address: str) -> int:
    """Register a notification channel for `owner`, returning its new id.

    Raises ValueError if this exact (owner, channel_type, address) is
    already registered.
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    INSERT,
                    {"owner": owner, "channel_type": channel_type, "address": address},
                )
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                raise ValueError(
                    f"{channel_type} channel {address!r} is already registered"
                ) from exc
            row = cur.fetchone()
        conn.commit()

    assert row is not None  # INSERT ... RETURNING id always returns a row
    logger.info(f"Added {channel_type} channel for {owner!r}")
    return row[0]


def get_channels_by_owner(owner: str) -> list[dict[str, Any]]:
    """List every notification channel registered by `owner`."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM notification_channels WHERE owner = %(owner)s ORDER BY created_at DESC",
            {"owner": owner},
        )
        return cur.fetchall()


def remove_channel(channel_id: int, owner: str) -> bool:
    """Remove a channel. Returns False if no matching channel was owned by
    `owner` (either it doesn't exist or belongs to someone else).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM notification_channels WHERE id = %(id)s AND owner = %(owner)s",
                {"id": channel_id, "owner": owner},
            )
            removed = cur.rowcount > 0
        conn.commit()
    return removed
