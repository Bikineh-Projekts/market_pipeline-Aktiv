"""
Database access.

Two things differ from the original project:

  1. DATABASE_URL is supported. Neon (and every other managed Postgres)
     hands you one connection string; splitting it into five secrets is
     needless work and an easy place to typo.

  2. One connection is shared for the whole run instead of opening a new
     one per collector call. On a scale-to-zero database each connect is
     a round trip to a compute that may need waking, so this matters.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import psycopg2

from . import config

logger = logging.getLogger(__name__)

_connection = None
_dim_cache: dict[str, int] = {}


def connect():
    """Open a new connection. Prefers DATABASE_URL when it is set."""
    if config.DATABASE_URL:
        return psycopg2.connect(config.DATABASE_URL, connect_timeout=20)

    return psycopg2.connect(
        host=config.PGHOST,
        port=config.PGPORT,
        dbname=config.PGDATABASE,
        user=config.PGUSER,
        password=config.PGPASSWORD,
        sslmode=config.PGSSLMODE,
        connect_timeout=20,
    )


def get_connection():
    """The shared connection for this process, reconnecting if it dropped."""
    global _connection

    if _connection is None or _connection.closed:
        _connection = connect()
        _connection.autocommit = False

    return _connection


def close_connection() -> None:
    global _connection
    if _connection is not None and not _connection.closed:
        _connection.close()
    _connection = None
    _dim_cache.clear()


@contextmanager
def cursor(commit: bool = True):
    """Cursor on the shared connection. Rolls back on error."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise


def apply_sql_file(path: str) -> None:
    """Run a .sql file. Used by tests and the local setup script."""
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with cursor() as cur:
        cur.execute(sql)


# ---------------------------------------------------------------------
# dimension helpers
# ---------------------------------------------------------------------
def _upsert_dim(table: str, key_col: str, key_val: str, pk_col: str, extra: dict | None = None) -> int:
    """Fetch-or-create a dimension row and return its surrogate key."""
    cache_key = f"{table}:{key_val}"
    if cache_key in _dim_cache:
        return _dim_cache[cache_key]

    extra = {k: v for k, v in (extra or {}).items() if v is not None}
    cols = [key_col] + list(extra.keys())
    vals = [key_val] + list(extra.values())

    placeholders = ", ".join(["%s"] * len(vals))
    col_list = ", ".join(cols)
    updates = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in cols if c != key_col)

    if updates:
        conflict = f"DO UPDATE SET {updates}"
    else:
        conflict = "DO NOTHING"

    sql = f"""
        INSERT INTO {table} ({col_list}) VALUES ({placeholders})
        ON CONFLICT ({key_col}) {conflict}
        RETURNING {pk_col};
    """

    with cursor() as cur:
        cur.execute(sql, vals)
        row = cur.fetchone()
        if row is None:  # DO NOTHING fired because the row already existed
            cur.execute(f"SELECT {pk_col} FROM {table} WHERE {key_col} = %s", (key_val,))
            row = cur.fetchone()

    _dim_cache[cache_key] = row[0]
    return row[0]


def source_id(name: str) -> int:
    return _upsert_dim("dim_source", "source_name", name, "source_id")


def symbol_id(code: str, **profile) -> int:
    return _upsert_dim("dim_symbol", "symbol_code", code.upper(), "symbol_id", profile)


def interval_id(code: str) -> int:
    return _upsert_dim("dim_interval", "interval_code", code, "interval_id")


def indicator_id(name: str) -> int:
    return _upsert_dim("dim_indicator", "indicator_name", name, "indicator_id")


# ---------------------------------------------------------------------
# api call logging
# ---------------------------------------------------------------------
def log_api_call(
    source: str,
    symbol: str | None,
    endpoint: str,
    http_status: int | None,
    response_ms: int | None,
    error: str | None = None,
    run_id: int | None = None,
) -> None:
    """
    Record one outbound API call.

    This is written for BOTH successes and failures. The original project
    only logged after a successful response, which meant log_api_call
    contained nothing but 200s and the success-rate metric was always 100%.
    """
    try:
        with cursor() as cur:
            cur.execute(
                """
                INSERT INTO log_api_call
                    (run_id, source_id, symbol_id, endpoint, http_status, response_ms, error_msg)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    source_id(source),
                    symbol_id(symbol) if symbol else None,
                    endpoint,
                    http_status,
                    response_ms,
                    (error or "")[:1000] or None,
                ),
            )
    except Exception as exc:  # logging must never break the pipeline
        logger.warning("could not write api log: %s", exc)


# ---------------------------------------------------------------------
# run logging
# ---------------------------------------------------------------------
def open_run(run_uid: str, trigger: str, symbols_total: int) -> int | None:
    try:
        with cursor() as cur:
            cur.execute(
                """
                INSERT INTO log_pipeline_run
                    (run_uid, trigger_source, started_at_utc, status, symbols_total)
                VALUES (%s, %s, NOW(), 'running', %s)
                ON CONFLICT (run_uid)
                DO UPDATE SET started_at_utc = NOW(), status = 'running'
                RETURNING run_id;
                """,
                (run_uid, trigger, symbols_total),
            )
            return cur.fetchone()[0]
    except Exception as exc:
        logger.warning("could not open run log: %s", exc)
        return None


def close_run(
    run_id: int | None,
    status: str,
    duration: float,
    symbols_ok: int,
    symbols_failed: int,
    rows_written: int,
    error: str | None,
) -> None:
    if run_id is None:
        return
    try:
        with cursor() as cur:
            cur.execute(
                """
                UPDATE log_pipeline_run
                   SET finished_at_utc  = NOW(),
                       duration_seconds = %s,
                       status           = %s,
                       symbols_ok       = %s,
                       symbols_failed   = %s,
                       rows_written     = %s,
                       error_msg        = %s
                 WHERE run_id = %s
                """,
                (duration, status, symbols_ok, symbols_failed, rows_written,
                 (error or "")[:2000] or None, run_id),
            )
    except Exception as exc:
        logger.warning("could not close run log: %s", exc)
