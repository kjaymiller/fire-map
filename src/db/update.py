import logging
import time
import uuid

from geojson import FeatureCollection

from src.viirs.get_fire_data import get_fire_data

from . import RELOAD_LOCK_TTL_SECONDS, UPDATE_INTERVAL_SECONDS, cache, notify, subscribers
from .postgres import init_db, insert_detections

logger = logging.getLogger(__name__)

# How often to check whether an in-flight reload elsewhere has landed, while
# waiting on it instead of duplicating it -- see reload_data_guarded.
RELOAD_WAIT_POLL_SECONDS = 0.2


def reload_data(ttl: int = UPDATE_INTERVAL_SECONDS) -> FeatureCollection:
    """Fetch fresh (global) fire data, append it to Postgres history under a
    single scan_id, and refresh the Valkey "current" cache. Returns the
    snapshot that was cached.
    """
    scan_id = uuid.uuid7()
    logger.info(f"Reloading fire data ({scan_id=})")
    features = list(get_fire_data())

    if not features:
        # FIRMS occasionally returns an empty response for a fetch (rate
        # limiting, a transient error, a day-boundary hiccup). Nothing gets
        # written to Postgres in that case (see insert_detections), so don't
        # blow away a good cached snapshot with an empty one either -- just
        # keep serving what's already cached until the next reload succeeds.
        logger.warning(f"Fetched 0 detections ({scan_id=}); keeping existing cache")
        cached = cache.get_current()
        if cached is not None:
            return cached
        return FeatureCollection(features)

    insert_detections(features, scan_id=scan_id)

    feature_collection = FeatureCollection(features)
    feature_collection["scan_id"] = str(scan_id)
    cache.set_current(feature_collection, ttl=ttl)

    queued = notify.queue_notifications(features, scan_id=scan_id)
    if queued:
        logger.info(f"Queued {queued} location alerts for ({scan_id=})")

    # Ephemeral point subscriptions (the map's "notify me about this area"
    # flow) only last as long as this reload's live snapshot still shows a
    # detection within their radius -- see subscribers.expire_ephemeral.
    # Only reached once features is confirmed non-empty (see the early
    # return above), so a transient empty FIRMS response can't wipe out
    # every ephemeral subscription on its own.
    active_points = [
        (feature["geometry"]["coordinates"][1], feature["geometry"]["coordinates"][0])
        for feature in features
    ]
    expired = subscribers.expire_ephemeral(active_points)
    if expired:
        logger.info(f"Expired {len(expired)} ephemeral subscription(s) ({scan_id=})")

    logger.info(f"Reload complete: {len(features)} detections ({scan_id=})")
    return feature_collection


def reload_data_guarded(ttl: int = UPDATE_INTERVAL_SECONDS) -> FeatureCollection:
    """Like reload_data, but only one reload actually runs at a time --
    across every request, thread, and web process/container, not just this
    one caller.

    index()/current()/lifespan() in web/app.py each independently reload on
    a cache miss with no coordination between them, and reload_data() does
    a live FIRMS fetch plus Postgres/Valkey writes -- slow enough, and
    blocking enough (network + DB I/O releases the GIL), that two requests
    hitting a cold cache at once would otherwise both actually run it:
    double the Postgres scan rows and, if FIRMS' response shifts between
    the two live fetches, double the queued notifications too. A caller
    that loses the race here waits for the winner's reload to land in the
    cache instead.
    """
    if cache.acquire_reload_lock(RELOAD_LOCK_TTL_SECONDS):
        try:
            return reload_data(ttl=ttl)
        finally:
            cache.release_reload_lock()

    logger.info("Reload already in progress elsewhere; waiting for it instead of duplicating it")
    deadline = time.monotonic() + RELOAD_LOCK_TTL_SECONDS
    while time.monotonic() < deadline:
        cached = cache.get_current()
        if cached is not None:
            return cached
        time.sleep(RELOAD_WAIT_POLL_SECONDS)

    # The winner's reload didn't land before our own wait ran out (it's
    # taking longer than RELOAD_LOCK_TTL_SECONDS, or it crashed after
    # claiming the lock but before the TTL expired) -- fall back to doing
    # it ourselves rather than serving nothing indefinitely.
    logger.warning("Timed out waiting for in-flight reload; reloading directly")
    return reload_data(ttl=ttl)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    reload_data()
