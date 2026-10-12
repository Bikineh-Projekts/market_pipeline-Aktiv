"""
Shared fixtures.

The suite runs against a real PostgreSQL, not a mock or SQLite: the schema
leans on jsonb, ON CONFLICT, window functions and generate_series, so
anything less would test a different database than production uses.

Point TEST_DATABASE_URL at a throwaway database. In CI that is the Postgres
service container; locally, any scratch database will do. The suite
truncates tables between tests, so never aim it at real data -- it refuses
to run against a database whose name does not look disposable.
"""

from __future__ import annotations

import os
import pathlib
import sys

import psycopg2
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:5433/market_pipeline_test",
)

SAFE_NAME_HINTS = ("test", "tmp", "scratch", "ci")

TABLES = [
    "log_api_call", "log_pipeline_run", "fact_market_indicator",
    "fact_market_timeseries", "fact_market_quote",
    "fact_company_fundamental", "fact_earnings_calendar", "dim_symbol",
]


def _guard(url: str) -> None:
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not any(h in name.lower() for h in SAFE_NAME_HINTS):
        pytest.exit(
            f"Refusing to run: TEST_DATABASE_URL points at '{name}', which does "
            f"not look like a throwaway database. The suite truncates tables. "
            f"Include one of {SAFE_NAME_HINTS} in the database name.",
            returncode=1,
        )


@pytest.fixture(scope="session", autouse=True)
def database():
    """Apply the real schema files once, to the real thing."""
    _guard(TEST_URL)

    try:
        conn = psycopg2.connect(TEST_URL, connect_timeout=10)
    except psycopg2.OperationalError as exc:
        pytest.exit(
            f"Cannot reach the test database.\n  {TEST_URL.rsplit('@', 1)[-1]}\n"
            f"  {exc}\nStart a throwaway Postgres and set TEST_DATABASE_URL.",
            returncode=1,
        )

    conn.autocommit = True
    with conn.cursor() as cur:
        for name in ("sql/01_schema.sql", "sql/02_bi_views.sql"):
            cur.execute((REPO / name).read_text())
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_tables(database):
    """Empty the facts before each test; keep the seeded dimensions."""
    with database.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def provider():
    """A running fake Finnhub / Twelve Data / Alpha Vantage."""
    from tests.mock_provider import MockProvider

    with MockProvider() as mock:
        yield mock


@pytest.fixture
def pipeline(provider, monkeypatch):
    """
    The real pipeline package, wired to the mock provider and test database.

    Collector module state and the db connection are reset per test so one
    test's cached dimension ids or open connection cannot leak into another.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("SYMBOLS", "AAPL,MSFT")
    monkeypatch.setenv("STATUS_FILE", "/dev/null")
    for var in ("FINNHUB_API_KEY", "ALPHAVANTAGE_API_KEY", "TWELVEDATA_API_KEY"):
        monkeypatch.setenv(var, "test-key")
    # No real waiting in tests.
    for var in ("SLEEP_FINNHUB", "SLEEP_TWELVEDATA", "SLEEP_ALPHAVANTAGE", "HTTP_BACKOFF"):
        monkeypatch.setenv(var, "0")

    # Only config is reloaded. reload() rebinds a module's contents in place,
    # so every `from .. import config` reference sees the new values -- but
    # reloading the collectors would mint *new* ApiError/RateLimited classes,
    # and a test's `pytest.raises(ApiError)` would then be comparing against
    # the stale ones and never match.
    import importlib

    from pipeline import config, db, run
    from pipeline.collectors import alphavantage, base, finnhub, twelvedata

    importlib.reload(config)

    monkeypatch.setattr(finnhub, "BASE", provider.finnhub_base)
    monkeypatch.setattr(twelvedata, "BASE", provider.twelvedata_base)
    monkeypatch.setattr(alphavantage, "BASE", provider.alphavantage_base)

    # Module-level state that must not leak between tests.
    db.close_connection()
    db._dim_cache.clear()

    class Harness:
        """Everything a test needs, in one object."""
        def __init__(self):
            self.run = run
            self.db = db
            self.config = config
            self.finnhub = finnhub
            self.twelvedata = twelvedata
            self.alphavantage = alphavantage
            self.base = base
            self.provider = provider

        def query(self, sql, params=None):
            with db.cursor(commit=False) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

        def scalar(self, sql, params=None):
            rows = self.query(sql, params)
            return rows[0][0] if rows else None

        def last_run(self):
            rows = self.query(
                "SELECT status, symbols_ok, symbols_failed, COALESCE(error_msg, '') "
                "FROM log_pipeline_run ORDER BY run_id DESC LIMIT 1"
            )
            return rows[0] if rows else None

    harness = Harness()
    yield harness
    db.close_connection()
