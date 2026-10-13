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

from . import auth, config, redact

logger = logging.getLogger(__name__)

_connection = None
_dim_cache: dict[str, int] = {}


def connect():
    """
    Open a new connection.

    With Lakebase OAuth configured, the password in DATABASE_URL is replaced
    by a freshly minted token; everything else about the string is kept. A
    native Postgres password role needs none of that, because those
    passwords do not expire -- see pipeline/auth.py.
    """
    # options sets the session time zone at startup. Without it the session
    # runs in the server's zone (UTC on Render), and then CURRENT_DATE and
    # every `timestamptz::date` cast resolve to the UTC day -- so a row
    # written at 23:30 UTC is filed under the previous Berlin day. The
    # stored instants are unaffected either way; this decides which
    # calendar day they are counted in.
    if config.DATABASE_URL:
        url = config.DATABASE_URL
        if config.lakebase_oauth_enabled():
            url = auth.apply_token(url, auth.database_token())
        return psycopg2.connect(
            url, connect_timeout=20, options=config.PG_SESSION_OPTIONS
        )

    password = config.PGPASSWORD
    user = config.PGUSER
    if config.lakebase_oauth_enabled():
        password = auth.database_token()
        user = user or config.DATABRICKS_CLIENT_ID

    return psycopg2.connect(
        host=config.PGHOST,
        port=config.PGPORT,
        dbname=config.PGDATABASE,
        user=user,
        password=password,
        sslmode=config.PGSSLMODE,
        connect_timeout=20,
        options=config.PG_SESSION_OPTIONS,
    )


def get_connection():
    """
    The shared connection for this process, reconnecting if it dropped.

    Under OAuth the credential expires after an hour, which a backfill can
    outlast. When the cached token nears expiry the connection is replaced
    rather than left to fail on its next query.
    """
    global _connection

    if (_connection is not None and not _connection.closed
            and config.lakebase_oauth_enabled() and not auth.token_is_fresh()):
        logger.info("Lakebase credential is close to expiry, reconnecting")
        try:
            _connection.close()
        except Exception:  # a dead connection is what we are replacing
            pass
        _connection = None

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
    auth.reset_cache()


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
    """
    Fetch-or-create a dimension row and return its surrogate key.

    The cache only short-circuits lookups that carry no new attributes.
    Serving a cache hit to a call that DOES carry attributes would skip the
    write entirely: within one run, collect_quote looks up the symbol with
    no profile and caches the id, then collect_fundamentals passes the real
    company name, exchange and country -- and those would never be stored,
    leaving dim_symbol permanently NULL and every Power BI slicer reading
    "UNKNOWN" / "Uncategorized".
    """
    extra = {k: v for k, v in (extra or {}).items() if v is not None}

    cache_key = f"{table}:{key_val}"
    if not extra and cache_key in _dim_cache:
        return _dim_cache[cache_key]
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
                    # Scrubbed here, at the only place error text enters
                    # this table, rather than at each caller. requests puts
                    # the whole request URL into the string form of a
                    # connection error, and Twelve Data and Alpha Vantage
                    # carry the API key in the query string -- so an
                    # unscrubbed message writes a live key into a column
                    # that the dashboard, the Power BI model and the public
                    # data export all read.
                    redact.scrub(error, limit=1000) or None,
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
                # Same reason as log_api_call, plus one of its own: a
                # fatal error here can be a psycopg2 failure whose message
                # contains the whole DSN, password included.
                (duration, status, symbols_ok, symbols_failed, rows_written,
                 redact.scrub(error, limit=2000) or None, run_id),
            )
    except Exception as exc:
        logger.warning("could not close run log: %s", exc)
