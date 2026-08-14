import logging
import os
import pathlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import dotenv
import fastapi
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from more_itertools import bucket
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from src import otel
from src.db import (
    DEFAULT_ALERT_RADIUS_MILES,
    HISTORY_AREA_CACHE_KEY_PREFIX,
    HISTORY_AREA_CACHE_TTL_SECONDS,
    cache,
    channels,
    notification_log,
    subscribers,
    users,
)
from src.db.postgres import get_history, get_history_in_area, get_scans, init_db
from src.db.update import reload_data

logger = logging.getLogger(__name__)

dotenv.load_dotenv()

# Must happen before the first Postgres connection or Valkey command --
# instruments the DB clients so every query/command gets its own span (see
# src/otel.py and src/otel_valkey.py).
otel.setup_telemetry(service_name="fire-map-web")


@asynccontextmanager
async def lifespan(_app: fastapi.FastAPI) -> AsyncIterator[None]:
    """Make sure the history/subscriber/user tables exist, and warm the
    cache if it's empty, before the app starts serving requests.
    """
    init_db()
    subscribers.init_db()
    users.init_db()
    channels.init_db()
    # Depends on subscribers' table already existing (FK) -- must come after.
    notification_log.init_db()
    if cache.get_current() is None:
        logger.info("Cache empty on startup, running an initial reload")
        reload_data()
    yield


api = fastapi.FastAPI(
    title="VIIRS Fire Detection API",
    version="0.3.0",
    lifespan=lifespan,
)

# One span per route, tagged with method/path/status -- alongside the DB
# spans above, this covers "every DB command + every route" end to end.
FastAPIInstrumentor.instrument_app(api)

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# *** DEMO-ONLY auth. DO NOT USE THIS IN PRODUCTION. ***
# Two ways in, both checked against the same salted-hash table in
# src/db/users.py -- see the caveat there in full:
#   - A signed session cookie (login()/logout() below) for the browser
#     pages -- log in once, the cookie carries you across /notify, /manage,
#     etc.
#   - HTTP Basic, checked fresh on every request, for curl/worker-style
#     access that never visits /login (this is what /subscribers/mine and
#     friends were built against before the login flow existed).
# Neither is session-store-backed or rate-limited; the cookie is just a
# signed username, so logging out one tab doesn't revoke the token itself
# -- there's nothing server-side to revoke it *from*.
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    SESSION_SECRET_KEY = secrets.token_hex(32)
    logger.warning(
        "SESSION_SECRET_KEY not set -- using a random key generated for "
        "this process. Every restart invalidates existing sessions; set "
        "SESSION_SECRET_KEY in .env to keep logins across restarts."
    )

api.add_middleware(
    SessionMiddleware, secret_key=SESSION_SECRET_KEY, session_cookie="firemap_session"
)

# Dependencies are built once at module scope (rather than calling
# `fastapi.Depends(...)` inline in each signature) so every route shares
# the same dependency instance instead of constructing a new one per call.
# `auto_error=False` so a request with no Basic header falls through to the
# session-cookie check below instead of 401ing immediately.
security = HTTPBasic(auto_error=False)
_credentials_dep = fastapi.Depends(security)


def get_session_user(request: Request) -> str | None:
    """The logged-in username for this browser session, or None."""
    return request.session.get("username")


def get_current_user(
    request: Request,
    credentials: HTTPBasicCredentials | None = _credentials_dep,
) -> str:
    session_username = get_session_user(request)
    if session_username:
        return session_username

    if credentials is not None and users.verify_user(credentials.username, credentials.password):
        return credentials.username

    raise fastapi.HTTPException(
        status_code=401,
        detail="Not logged in",
        headers={"WWW-Authenticate": "Basic"},
    )


CurrentUser = fastapi.Depends(get_current_user)


def get_confidence_breakdown(features: list[dict[str, Any]]) -> dict[str, int]:
    """Count detections per confidence level."""
    if not features:
        return {}

    batches = bucket(features, key=lambda feature: feature["properties"]["confidence"])
    return {key: len(list(batches[key])) for key in list(batches)}


@api.get("/health")
def health() -> dict[str, str]:
    """Liveness/readiness probe -- no DB or cache access, just confirms the
    app has finished startup (lifespan) and uvicorn is accepting requests.
    """
    return {"status": "ok"}


@api.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Render the homepage using the current snapshot from Valkey."""
    feature_collection = cache.get_current()

    if feature_collection is None:
        feature_collection = reload_data()

    confidences = get_confidence_breakdown(feature_collection["features"])
    scans = get_scans(limit=1)
    latest_run = scans[0] if scans else None

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "points": feature_collection,
            "data": len(feature_collection["features"]),
            "confidences": confidences,
            "latest_run": latest_run,
            "current_user": get_session_user(request),
        },
    )


@api.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    """Create-account page. Posts to the /register API below."""
    return templates.TemplateResponse(
        request, "register.html", {"current_user": get_session_user(request)}
    )


@api.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """Sign-in page. Posts to the /login API below."""
    return templates.TemplateResponse(
        request, "login.html", {"current_user": get_session_user(request)}
    )


@api.get("/notify", response_class=HTMLResponse)
def notify_page(request: Request) -> HTMLResponse:
    """Page for registering a new location + radius alert subscription.
    Requires an account (see /register) -- posts to /subscribers.
    """
    return templates.TemplateResponse(
        request, "subscribe.html", {"current_user": get_session_user(request)}
    )


@api.get("/manage", response_class=HTMLResponse)
def manage_page(request: Request) -> HTMLResponse:
    """Page for viewing and cancelling your own subscriptions. Reads from
    /subscribers/mine and deletes via /subscribers/{id}.
    """
    return templates.TemplateResponse(
        request, "manage.html", {"current_user": get_session_user(request)}
    )


@api.get("/manage-notifications", response_class=HTMLResponse)
def manage_notifications_page(request: Request) -> HTMLResponse:
    """Page for managing the channels (email, Discord) your location alerts
    (see /notify) actually get delivered to. Requires an account -- reads
    from /channels/mine and posts to /channels.
    """
    return templates.TemplateResponse(
        request, "manage_notifications.html", {"current_user": get_session_user(request)}
    )


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)


@api.post("/login")
def login(payload: LoginRequest, request: Request) -> dict[str, str]:
    """Verify credentials and start a session (see the auth caveat above
    `get_current_user`).
    """
    if not users.verify_user(payload.username, payload.password):
        raise fastapi.HTTPException(status_code=401, detail="Invalid username or password")
    request.session["username"] = payload.username
    return {"username": payload.username}


@api.post("/logout")
def logout(request: Request) -> dict[str, str]:
    """End the current session."""
    request.session.clear()
    return {"status": "logged out"}


@api.get("/me")
def me(request: Request) -> dict[str, str | None]:
    """The current session's username, or None. No 401 on logged-out --
    pages poll this to decide what to show.
    """
    return {"username": get_session_user(request)}


@api.post("/reload")
def reload() -> JSONResponse:
    """Fetch fresh (global) data, persist it, and return the snapshot read back from Valkey."""
    reload_data()
    return JSONResponse(cache.get_current())


@api.get("/current")
def current() -> JSONResponse:
    """Return the current snapshot from Valkey, reloading once if it has expired."""
    feature_collection = cache.get_current()

    if feature_collection is None:
        reload_data()
        feature_collection = cache.get_current()

    return JSONResponse(feature_collection)


@api.get("/history")
def history(
    limit: int = 500,
    since: str | None = None,
    scan_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return historical detections from Postgres, optionally scoped to one scan.

    Returned as a plain object (rather than JSONResponse) so FastAPI's
    jsonable_encoder can serialize the datetime/UUID values psycopg hands back.
    """
    return get_history(limit=limit, since=since, scan_id=scan_id)


@api.get("/scans")
def scans(limit: int = 50) -> list[dict[str, Any]]:
    """List recent scans (one entry per reload) for grouping history by collection event."""
    return get_scans(limit=limit)


@api.get("/history/area")
def history_area(
    lat: float = fastapi.Query(..., ge=-90, le=90),
    lon: float = fastapi.Query(..., ge=-180, le=180),
    radius_miles: float = fastapi.Query(..., gt=0, le=500),
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Historical detections within `radius_miles` of a point, nearest
    first -- the query behind the map's "show history here" button, once a
    viewer has zoomed in far enough to make one area's history meaningful.

    Not cached: a map viewport is a different (lat, lon, radius) on
    basically every call (pan a pixel, zoom a notch), so there's no real
    "same point" for a cache to hit -- see subscriber_history below for
    the query this app actually can cache, against the fixed, known set of
    subscriber locations.
    """
    return get_history_in_area(latitude=lat, longitude=lon, radius_miles=radius_miles, limit=limit)


def _subscriber_history_cache_key(subscriber_id: int) -> str:
    return f"{HISTORY_AREA_CACHE_KEY_PREFIX}subscriber:{subscriber_id}"


@api.get("/subscribers/{subscriber_id}/history")
def subscriber_history(
    subscriber_id: int,
    limit: int = 500,
    username: str = CurrentUser,
) -> list[dict[str, Any]]:
    """Historical detections near one of your alert areas, nearest first.
    Requires login and ownership.

    Unlike /history/area above, this point is fixed -- the same
    subscription gets checked repeatedly (every /manage page load, anyone
    else viewing the same area) -- so it's cached in Valkey for
    HISTORY_AREA_CACHE_TTL_SECONDS, keyed on subscriber_id. See
    delete_subscriber below for cache invalidation on cancel.
    """
    sub = subscribers.get_subscriber(subscriber_id, owner=username)
    if sub is None:
        raise fastapi.HTTPException(
            status_code=404,
            detail="No subscription with that id owned by this account",
        )

    cache_key = _subscriber_history_cache_key(subscriber_id)
    cached = cache.get_cached_json(cache_key)
    if cached is not None:
        return cached

    rows = get_history_in_area(
        latitude=sub["latitude"],
        longitude=sub["longitude"],
        radius_miles=sub["radius_miles"],
        limit=limit,
    )
    encoded = jsonable_encoder(rows)
    cache.set_cached_json(cache_key, encoded, ttl=HISTORY_AREA_CACHE_TTL_SECONDS)
    return encoded


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8)


@api.post("/register")
def register(payload: RegisterRequest) -> dict[str, Any]:
    """Create a demo account. See the auth caveat on `get_current_user` --
    this is intentionally minimal and not meant to hold up in production.
    """
    try:
        user_id = users.create_user(payload.username, payload.password)
    except ValueError as exc:
        raise fastapi.HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": user_id, "username": payload.username}


class SubscriptionRequest(BaseModel):
    # Optional -- delivery goes to whichever channels you've registered on
    # /manage-notifications (see notifier.py), not to this field. It's kept
    # around as a free-text label on the subscription itself (shown on
    # /manage); defaults to your username when omitted.
    contact: str | None = Field(
        None, description="Optional label for this subscription. Defaults to your username."
    )
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    radius_miles: float = Field(DEFAULT_ALERT_RADIUS_MILES, gt=0, le=500)


@api.post("/subscribers")
def create_subscriber(
    payload: SubscriptionRequest,
    username: str = CurrentUser,
) -> dict[str, Any]:
    """Register a location + radius for fire alerts. Requires login.

    Every reload, freshly fetched detections within `radius_miles` of this
    point get queued as a notification and drained by the notifier service
    (see src/notifier.py), sent to whichever channels you've registered on
    /manage-notifications.
    """
    subscriber_id = subscribers.add_subscriber(
        owner=username,
        contact=payload.contact or username,
        latitude=payload.latitude,
        longitude=payload.longitude,
        radius_miles=payload.radius_miles,
    )
    return {"id": subscriber_id}


@api.get("/subscribers/mine")
def list_my_subscribers(username: str = CurrentUser) -> list[dict[str, Any]]:
    """List only the subscriptions you own. Requires login."""
    return subscribers.get_subscribers_by_owner(username)


@api.delete("/subscribers/{subscriber_id}")
def delete_subscriber(
    subscriber_id: int,
    username: str = CurrentUser,
) -> dict[str, str]:
    """Cancel a subscription you own. Requires login."""
    removed = subscribers.remove_subscriber(subscriber_id, owner=username)
    if not removed:
        raise fastapi.HTTPException(
            status_code=404,
            detail="No subscription with that id owned by this account",
        )
    # subscriber_id is a BIGSERIAL, never reused -- this is just tidiness
    # (the TTL would clear it anyway), not a correctness fix.
    cache.get_client().delete(_subscriber_history_cache_key(subscriber_id))
    return {"status": "removed"}


@api.get("/notification-events/mine")
def list_my_notification_events(
    limit: int = 200,
    username: str = CurrentUser,
) -> list[dict[str, Any]]:
    """List how your subscriptions have actually triggered -- one row per
    (subscription, detection) match, newest first, with whether it was
    delivered, failed, or had no channel to go to. Requires login.
    """
    return notification_log.get_events_by_owner(username, limit=limit)


class ChannelRequest(BaseModel):
    # Matches channels.DELIVERABLE_CHANNEL_TYPES -- sms/signal/telegram
    # aren't wired to an actual send yet (see src/db/channels.py), so
    # there's no point letting anyone register one that'll just be skipped.
    channel_type: Literal["email", "discord"]
    address: str = Field(
        ..., min_length=1, max_length=256, description="Address or handle for this channel."
    )


@api.post("/channels")
def create_channel(
    payload: ChannelRequest,
    username: str = CurrentUser,
) -> dict[str, Any]:
    """Register a notification channel (email or Discord) for your
    account. Requires login.
    """
    try:
        channel_id = channels.add_channel(
            owner=username,
            channel_type=payload.channel_type,
            address=payload.address,
        )
    except ValueError as exc:
        raise fastapi.HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": channel_id}


@api.get("/channels/mine")
def list_my_channels(username: str = CurrentUser) -> list[dict[str, Any]]:
    """List only the notification channels you own. Requires login."""
    return channels.get_channels_by_owner(username)


@api.delete("/channels/{channel_id}")
def delete_channel(
    channel_id: int,
    username: str = CurrentUser,
) -> dict[str, str]:
    """Remove a notification channel you own. Requires login."""
    removed = channels.remove_channel(channel_id, owner=username)
    if not removed:
        raise fastapi.HTTPException(
            status_code=404,
            detail="No channel with that id owned by this account",
        )
    return {"status": "removed"}
