"""Postgres (Supabase) connection pool shared by accounts (auth.py) and
saved user data (persist.py). Enabled when SUPABASE_DB_URL is set.

The tables live in their own schema, "bdc", which Supabase does not
expose through its REST API, and have row level security on with no
policies: the publishable / anon key can never read them. Only this
server - the connection string's user, which owns the tables - can.

Each process (API, collection worker) opens its own small pool on first
use and creates the schema if it is missing.
"""

from __future__ import annotations

import logging
import threading
import time

from . import config

log = logging.getLogger("db")

SCHEMA = "bdc"
POOL_SIZE = 3            # per process; Supabase's free pooler allows ~15 in all

_DDL = f"""
SELECT pg_advisory_xact_lock(727260412);
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.users (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.sessions (
    token_hash text PRIMARY KEY,
    user_id    text NOT NULL REFERENCES {SCHEMA}.users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expires_at ON {SCHEMA}.sessions (expires_at);
CREATE TABLE IF NOT EXISTS {SCHEMA}.user_files (
    owner      text NOT NULL,
    name       text NOT NULL,
    data       bytea NOT NULL,
    size       integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, name)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.auth_events (
    id      bigserial PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    kind    text NOT NULL,
    user_id text NOT NULL DEFAULT '',
    email   text NOT NULL DEFAULT '',
    ip      text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS auth_events_at ON {SCHEMA}.auth_events (at);
CREATE TABLE IF NOT EXISTS {SCHEMA}.user_keys (
    owner      text PRIMARY KEY,
    api_key    text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE {SCHEMA}.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.user_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.user_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.auth_events ENABLE ROW LEVEL SECURITY;
"""

_pool = None
_lock = threading.Lock()
# After a failed connect, requests fail at once for this long instead of
# each waiting for its own attempt: logins never pile up behind a database
# that is down or misconfigured (a proxy in front answers 502 to those).
RETRY_AFTER_S = 15.0
_failed_at = 0.0


def placeholder_url() -> bool:
    """SUPABASE_DB_URL still holds Supabase's [YOUR-PASSWORD] placeholder."""
    url = config.DATABASE_URL.lower()
    return "[your-password]" in url or "[password]" in url


def enabled() -> bool:
    return bool(config.DATABASE_URL)


# -- connection status for /api/health: says WHY the database can't be used
# (never the password), so a deploy with a wrong SUPABASE_DB_URL can be fixed
# without reading server logs.
STATUS_CACHE_S = 60.0
_status = {"t": 0.0, "value": ""}
_status_lock = threading.Lock()

_REASONS = (
    ("password authentication failed", "wrong database password in SUPABASE_DB_URL "
     "(special characters must be URL-encoded: @ -> %40, # -> %23, + -> %2B, & -> %26, / -> %2F)"),
    ("tenant or user not found", "wrong username or project in SUPABASE_DB_URL - the Session "
     "pooler user is postgres.<project-ref>, copied from Supabase -> Connect -> Session pooler"),
    ("could not translate host name", "database host not found - check the host in SUPABASE_DB_URL"),
    ("failed to resolve host", "database host not found - check the host in SUPABASE_DB_URL"),
    ("missing \"=\"", "SUPABASE_DB_URL is not a valid postgresql:// connection string"),
    ("name or service not known", "database host not found - check the host in SUPABASE_DB_URL"),
    ("network is unreachable", "database host not reachable - db.<ref>.supabase.co is IPv6-only: "
     "use the Session pooler string (Supabase -> Connect -> Session pooler)"),
    ("cannot assign requested address", "database host not reachable - db.<ref>.supabase.co is "
     "IPv6-only: use the Session pooler string (Supabase -> Connect -> Session pooler)"),
    ("timeout", "connection to the database timed out - check the host and port in SUPABASE_DB_URL"),
    ("timed out", "connection to the database timed out - check the host and port in SUPABASE_DB_URL"),
    ("connection refused", "the database refused the connection - check the port (5432) in SUPABASE_DB_URL"),
    ("invalid dsn", "SUPABASE_DB_URL is not a valid postgresql:// connection string"),
    ("invalid connection option", "SUPABASE_DB_URL is not a valid postgresql:// connection string"),
)


def _reason(exc: BaseException) -> str:
    msg = str(exc)
    low = msg.lower()
    for needle, text in _REASONS:
        if needle in low:
            return text
    from urllib.parse import urlsplit
    try:
        pw = urlsplit(config.DATABASE_URL).password or ""
    except ValueError:
        pw = ""
    first = (msg.strip().splitlines() or [""])[0]
    if pw:
        first = first.replace(pw, "***")
    return f"{type(exc).__name__}: {first[:200]}"


def status() -> str:
    """'off' (no SUPABASE_DB_URL), 'ok', or 'error: <why>'. Cached
    STATUS_CACHE_S. Blocking - call in a thread."""
    if not enabled():
        return "off"
    now = time.monotonic()
    with _status_lock:
        if _status["value"] and now - _status["t"] < STATUS_CACHE_S:
            return _status["value"]
    url = config.DATABASE_URL
    if placeholder_url():
        value = ("error: SUPABASE_DB_URL still contains the [YOUR-PASSWORD] placeholder - "
                 "put the real database password there (without the brackets)")
    else:
        try:
            if _pool is not None:
                with _pool.connection(timeout=10) as conn:
                    conn.execute("SELECT 1")
            else:
                import psycopg
                psycopg.connect(url, connect_timeout=10, prepare_threshold=None).close()
            value = "ok"
        except Exception as exc:
            value = "error: " + _reason(exc)
    with _status_lock:
        _status.update(t=time.monotonic(), value=value)
    if value != "ok":
        log.warning("database status: %s", value)
    return value


def pool():
    """The process's connection pool (opened + schema created on first use;
    raises if the database cannot be reached - the next call retries)."""
    global _pool, _failed_at
    if _pool is None:
        if placeholder_url():
            raise RuntimeError("SUPABASE_DB_URL still contains the [YOUR-PASSWORD] placeholder")
        if time.monotonic() - _failed_at < RETRY_AFTER_S:
            raise RuntimeError("database unavailable (the last connect attempt just failed)")
        with _lock:
            if _pool is None and time.monotonic() - _failed_at < RETRY_AFTER_S:
                raise RuntimeError("database unavailable (the last connect attempt just failed)")
            if _pool is None:
                from psycopg_pool import ConnectionPool
                p = ConnectionPool(
                    config.DATABASE_URL, min_size=1, max_size=POOL_SIZE,
                    # prepare_threshold=None: also works behind a
                    # transaction-mode pooler (Supabase port 6543)
                    kwargs={"connect_timeout": 10, "prepare_threshold": None},
                    check=ConnectionPool.check_connection, max_idle=300,
                    timeout=20, open=False, name="bdc")
                try:
                    p.open(wait=True, timeout=15)
                    with p.connection() as conn:
                        conn.execute(_DDL)
                except BaseException:
                    p.close()
                    _failed_at = time.monotonic()
                    raise
                _pool = p
                log.info("database connected")
    return _pool


def connection():
    """`with db.connection() as conn:` - one transaction, committed on exit."""
    return pool().connection()


def close():
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None
