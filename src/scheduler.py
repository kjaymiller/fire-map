"""Periodically triggers a reload by POSTing to the web app.

This replaces the old Azure Functions timer trigger — it runs as its own
docker-compose service instead of an in-process cron.
"""

import logging
import os
import time

import httpx
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from src import otel

logger = logging.getLogger(__name__)

WEB_URL = os.environ.get("WEB_URL", "http://web:8000")
UPDATE_INTERVAL_SECONDS = int(os.environ.get("UPDATE_INTERVAL_SECONDS", "900"))

# Belt-and-suspenders alongside docker-compose's `condition: service_healthy`
# on the web service: that keeps this container from starting before web is
# ready, but doesn't help if web restarts (e.g. a deploy) while we're
# already running. Retry a few times with backoff before giving up on a
# cycle instead of waiting the full interval on a single connection error.
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 2

# Instruments httpx so the trace context for each /reload POST propagates
# into the web app -- the route span there and the DB spans it triggers
# show up as children of the same trace this scheduler starts.
otel.setup_telemetry(service_name="fire-map-scheduler")
HTTPXClientInstrumentor().instrument()


def trigger_reload() -> None:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = httpx.post(f"{WEB_URL}/reload", timeout=60)
            logger.info(f"Reload triggered: {response.status_code}")
            return
        except httpx.HTTPError as exc:
            if attempt == RETRY_ATTEMPTS:
                logger.warning(f"Reload request failed after {attempt} attempts: {exc}")
                return
            logger.warning(f"Reload request failed (attempt {attempt}/{RETRY_ATTEMPTS}): {exc}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)


def run() -> None:
    while True:
        trigger_reload()
        time.sleep(UPDATE_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
