import os

import dotenv

dotenv.load_dotenv()

UPDATE_INTERVAL_SECONDS = int(os.environ.get("UPDATE_INTERVAL_SECONDS", "900"))

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "firemap")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "firemap")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "firemap")

POSTGRES_DSN = os.environ.get(
    "POSTGRES_DSN",
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}",
)

VALKEY_HOST = os.environ.get("VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.environ.get("VALKEY_PORT", "6379"))
VALKEY_URL = os.environ.get(
    "VALKEY_URL",
    f"valkey://{VALKEY_HOST}:{VALKEY_PORT}/0",
)

CURRENT_CACHE_KEY = "firemap:current"

# Each detection in a poll is cached under its own key (see cache.py), all
# sharing one TTL via MSETEX. NOTIFY_KEY_PREFIX namespaces the per-scan hash
# of pending location-alert notifications (see notify.py) that HGETDEL
# drains.
EVENT_KEY_PREFIX = "firemap:event:"
NOTIFY_KEY_PREFIX = "firemap:notify:"

# A list of scan_ids that have a non-empty NOTIFY_KEY_PREFIX hash waiting to
# be drained. The notifier service (src/notifier.py) blocks on this with
# BLPOP instead of polling every scan_id it can think of -- see
# notify.queue_notifications / notify.next_scan_id.
NOTIFY_SCAN_QUEUE_KEY = "firemap:notify:scans"

# Default radius (in miles) used for a subscription when the caller doesn't
# specify one.
DEFAULT_ALERT_RADIUS_MILES = 25.0

# Namespaces the cached result of a "history near this point" query (see
# postgres.get_history_in_area and the /history/area route) -- the PostGIS
# query behind it is the most expensive one in the app, and its inputs
# (map center + viewport radius) repeat heavily as one viewer pans/zooms
# around the same area, or as several viewers look at the same hotspot.
HISTORY_AREA_CACHE_KEY_PREFIX = "firemap:history_area:"

# History doesn't change until the next scheduled reload, so there's no
# point re-querying Postgres more often than that -- reuse the same
# interval the rest of the app already ties freshness to.
HISTORY_AREA_CACHE_TTL_SECONDS = int(
    os.environ.get("HISTORY_AREA_CACHE_TTL_SECONDS", str(UPDATE_INTERVAL_SECONDS))
)
