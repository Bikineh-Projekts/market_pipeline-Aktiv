"""
Database access for the web dashboard.

Two deliberate differences from pipeline/db.py:

  1. **Read-only, enforced by the database.** Every transaction is opened
     with SET TRANSACTION READ ONLY, so this service cannot write even if a
     future edit tried to. GitHub Actions is the only writer; the previous
     version of this project called create_schema() from its index page,
     which meant two processes wrote to one database, duplicating rows and
     burning the API quota twice as fast. Making it structurally impossible
     is better than remembering not to.

  2. **A small connection pool.** A web service answers overlapping
     requests, and a hosted Postgres allows a limited number of
     connections. Opening one per request exhausts them under any load.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool as pg_pool

logger = logging.getLogger(__name__)

_pool: pg_pool.ThreadedConnectionPool | None = None
_lock = threading.Lock()


class SchemaMissing(Exception):
    """The database is reachable but the pipeline's tables are not there."""


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # Render's external connection string omits sslmode, and psycopg2
        # then defaults to "prefer", whose fallback produces the unhelpful
        # "SSL connection has been closed unexpectedly". Be explicit.
        if "sslmode=" not in url and not _is_local(url):
            url += "&sslmode=require" if "?" in url else "?sslmode=require"
        return url

    host = os.getenv("PGHOST", "")
    if not host:
        return ""
    return (
        f"postgresql://{os.getenv('PGUSER', '')}:{os.getenv('PGPASSWORD', '')}"
        f"@{host}:{os.getenv('PGPORT', '5432')}/{os.getenv('PGDATABASE', '')}"
        f"?sslmode={os.getenv('PGSSLMODE', 'require')}"
    )


def _is_local(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            url = database_url()
            if not url:
                raise RuntimeError("DATABASE_URL is not set")
            _pool = pg_pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=int(os.getenv("WEB_DB_POOL_MAX", "4")),
                dsn=url,
                connect_timeout=10,
                # Keeps a spun-down free instance from inheriting a dead
                # socket when Render wakes it up again.
                keepalives=1,
                keepalives_idle=30,
            )
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def read_cursor():
    """
    A cursor in a read-only transaction, returned to the pool after use.

    Dead connections need no handling here: psycopg2's pool already closes
    one whose transaction status is UNKNOWN ("server connection lost") and
    discards one already closed, rather than handing it out again. What it
    does not do is retry, which is why `query` does — see there.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            yield cur
        conn.rollback()  # nothing to commit; a read-only txn just ends
    except Exception:
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass  # a lost connection cannot be rolled back
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            logger.warning("could not return a connection to the pool")


def _query_once(sql: str, params=None) -> list[dict]:
    with read_cursor() as cur:
        try:
            cur.execute(sql, params)
        except psycopg2.errors.UndefinedTable as exc:
            raise SchemaMissing(str(exc).splitlines()[0]) from exc
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def query(sql: str, params=None) -> list[dict]:
    """
    Rows as dicts, so templates and JSON need no column bookkeeping.

    Retried once on a connection-level error. A free Render instance is
    spun down after 15 minutes and its pooled sockets are dead when it
    wakes, so without this the first visit after any quiet period shows an
    error page and the second one works -- which looks like a broken site.
    The retry gets a new connection, because the dead one was discarded.
    """
    try:
        return _query_once(sql, params)
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
        logger.info("retrying after a stale connection: %s", str(exc).strip()[:120])
        return _query_once(sql, params)


def query_one(sql: str, params=None) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def schema_present() -> bool:
    """Whether migrate has been run against this database."""
    row = query_one(
        """
        SELECT COUNT(*) AS n
          FROM information_schema.views
         WHERE table_schema = 'public' AND table_name LIKE 'bi\\_%'
        """
    )
    return bool(row and row["n"] > 0)
