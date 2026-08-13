"""Minimal username/password accounts, used only to gate the notification
endpoints (see notify.py / subscribers.py).

*** DEMO-ONLY. DO NOT USE THIS AUTH IN PRODUCTION. ***
There's no session/token layer, no rate limiting on login attempts, no
password strength or reuse checks, and no account recovery. Credentials are
checked fresh on every request via HTTP Basic against a salted PBKDF2 hash
stored in Postgres -- fine for keeping a demo app's notification signup
from being open to anyone, not a substitute for a real auth system.
"""

import hashlib
import hmac
import logging
import secrets

import psycopg

from .postgres import get_pool

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# On the high end for interactive logins, but this table is never going to
# see real load -- err toward slower/safer.
PBKDF2_ITERATIONS = 310_000


def init_db() -> None:
    """Create the users table if it doesn't already exist."""
    logger.info("Ensuring users table exists")
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (salt, hash) for a password. Generates a fresh random salt
    unless one is passed in (for verifying against an existing hash).
    """
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS
    )
    return salt, digest.hex()


def create_user(username: str, password: str) -> int:
    """Register a new user. Raises ValueError if the username is taken."""
    salt, password_hash = hash_password(password)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO users (username, salt, password_hash) "
                    "VALUES (%(username)s, %(salt)s, %(password_hash)s) RETURNING id",
                    {"username": username, "salt": salt, "password_hash": password_hash},
                )
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                raise ValueError(f"Username {username!r} is already taken") from exc
            row = cur.fetchone()
        conn.commit()

    assert row is not None  # INSERT ... RETURNING id always returns a row
    logger.info(f"Registered user {username!r}")
    return row[0]


def verify_user(username: str, password: str) -> bool:
    """Check a username/password pair against the stored salted hash."""
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT salt, password_hash FROM users WHERE username = %(username)s",
            {"username": username},
        )
        row = cur.fetchone()

    if row is None:
        # Still hash something so a nonexistent username doesn't return
        # faster than a wrong password would (a cheap timing-attack guard).
        hash_password(password)
        return False

    salt, expected_hash = row
    _, candidate_hash = hash_password(password, salt=salt)
    return hmac.compare_digest(candidate_hash, expected_hash)
