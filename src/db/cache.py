"""Valkey-backed cache for the *current* fire detection snapshot.

Every detection in a poll is cached under its own key, plus one index key
listing them, and all of those keys are written in a single MSETEX call so
they share one TTL. That's the point: previously this was a SET+EXPIRE pair
per key (or a pipeline of them), which left a window where some keys of the
same poll could expire a beat apart from the others. MSETEX makes the whole
poll expire together, right around the time the next scheduled reload is
due -- a stale key is a signal that a reload is overdue rather than
something tracked separately.
"""

import json
import logging
from typing import Any, cast

import valkey
from geojson import Feature, FeatureCollection

from . import CURRENT_CACHE_KEY, EVENT_KEY_PREFIX, UPDATE_INTERVAL_SECONDS, VALKEY_URL

logger = logging.getLogger(__name__)

_client: valkey.Valkey | None = None


def get_client() -> valkey.Valkey:
    """Lazily create (and reuse) the Valkey client."""
    global _client
    if _client is None:
        _client = valkey.Valkey.from_url(VALKEY_URL, decode_responses=True)
    return _client


def event_id(feature: Feature) -> str:
    """A stable id for a detection within a scan, used as its cache key.

    Detections don't come back from FIRMS with an id of their own, so this
    is built from the fields that together identify one satellite pass over
    one point: where and when it was seen, and by what.
    """
    props = feature["properties"]
    lon, lat = feature["geometry"]["coordinates"]
    return f"{lat:.5f}:{lon:.5f}:{props['datetime']}:{props['satellite']}"


def msetex(mapping: dict[str, str], ttl: int) -> None:
    """Set every key in `mapping` with one shared TTL in a single MSETEX call."""
    if not mapping:
        return

    args = []
    for key, value in mapping.items():
        args.extend((key, value))

    get_client().execute_command("MSETEX", len(mapping), *args, "EX", ttl)


def set_current(
    feature_collection: FeatureCollection,
    ttl: int = UPDATE_INTERVAL_SECONDS,
) -> None:
    """Cache every detection from this poll as its own key, plus an index of
    those keys, all sharing one TTL via MSETEX.
    """
    features = feature_collection["features"]
    logger.info(f"Caching {len(features)} detections in Valkey via MSETEX (ttl={ttl}s)")

    mapping = {
        f"{EVENT_KEY_PREFIX}{event_id(feature)}": json.dumps(feature)
        for feature in features
    }
    mapping[CURRENT_CACHE_KEY] = json.dumps(
        {
            "scan_id": feature_collection.get("scan_id"),
            "keys": list(mapping.keys()),
        }
    )
    msetex(mapping, ttl)


def get_current() -> FeatureCollection | None:
    """Rebuild the current snapshot from its individually-cached events.

    Returns None once the poll's keys have expired (or before the first
    poll has ever run).
    """
    client = get_client()
    # valkey's command methods are typed to also cover its async client
    # (return type `Awaitable[Any] | Any`), even though `get_client()` only
    # ever hands back the synchronous one -- the casts below just narrow
    # back to what actually comes back at runtime here.
    raw_index = cast("str | None", client.get(CURRENT_CACHE_KEY))
    if raw_index is None:
        return None

    index = json.loads(raw_index)
    keys = [key for key in index["keys"] if key != CURRENT_CACHE_KEY]
    raw_events = cast("list[str | None]", client.mget(keys)) if keys else []

    features = [json.loads(raw) for raw in raw_events if raw is not None]
    feature_collection = FeatureCollection(features)
    feature_collection["scan_id"] = index.get("scan_id")
    return feature_collection


def get_cached_json(key: str) -> Any | None:
    """Fetch a JSON-encoded value cached under `key`, or None if it isn't
    cached (or already expired). Generic -- unlike set_current/get_current
    above, this doesn't know anything about the shape of what it's caching.
    """
    raw = cast("str | None", get_client().get(key))
    if raw is None:
        return None
    return json.loads(raw)


def set_cached_json(key: str, value: Any, ttl: int) -> None:
    """Cache a JSON-serializable value under `key` for `ttl` seconds."""
    get_client().set(key, json.dumps(value), ex=ttl)
