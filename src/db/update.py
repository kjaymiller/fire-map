import logging
import uuid

from geojson import FeatureCollection

from src.viirs.get_fire_data import get_fire_data

from . import UPDATE_INTERVAL_SECONDS, cache, notify
from .postgres import init_db, insert_detections

logger = logging.getLogger(__name__)


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

    logger.info(f"Reload complete: {len(features)} detections ({scan_id=})")
    return feature_collection


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    reload_data()
