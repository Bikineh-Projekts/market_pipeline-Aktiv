"""
The Power BI layer.

These views are the project's actual product: if a category column turns
to NULL or a derived number goes wrong, the report is wrong even though
every row loaded fine.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


def _populate(pipeline):
    pipeline.run.main(["--full", "--trigger", "test"])


def test_price_daily_has_a_row_per_symbol_and_day(pipeline):
    _populate(pipeline)
    rows = pipeline.scalar("SELECT COUNT(*) FROM bi_fact_price_daily")
    distinct = pipeline.scalar(
        "SELECT COUNT(*) FROM (SELECT DISTINCT symbol_id, date_key "
        "FROM bi_fact_price_daily) x")
    assert rows == distinct, "duplicate (symbol, day) rows would double every measure"


def test_change_pct_matches_the_closes(pipeline):
    _populate(pipeline)
    row = pipeline.query("""
        SELECT close, prev_close, change_pct
          FROM bi_fact_price_daily
         WHERE prev_close IS NOT NULL
         LIMIT 1
    """)[0]
    close, prev, pct = (Decimal(str(v)) for v in row)
    expected = (close - prev) / prev * 100
    assert abs(pct - expected) < Decimal("0.01")


def test_move_category_agrees_with_change_pct(pipeline):
    _populate(pipeline)
    bad = pipeline.query("""
        SELECT change_pct, move_category FROM bi_fact_price_daily
         WHERE prev_close IS NOT NULL
           AND ((change_pct > 0 AND move_category NOT IN ('Up','Strong Up'))
             OR (change_pct < 0 AND move_category NOT IN ('Down','Strong Down')))
    """)
    assert bad == [], f"category contradicts the number: {bad[:3]}"


def test_no_category_column_is_ever_null(pipeline):
    """
    A NULL in a slicer column shows up as a blank filter entry in Power BI.
    Every category column uses COALESCE or CASE for exactly that reason.
    """
    checks = [
        ("bi_dim_symbol", ["exchange", "country", "currency", "sector",
                           "industry", "market_cap_band", "valuation_band",
                           "volatility_band"]),
        ("bi_fact_price_daily", ["move_category"]),
        ("bi_fact_indicator_daily", ["rsi_signal", "macd_signal_category",
                                     "trend_category"]),
        ("bi_fact_api_call", ["status_category", "latency_band", "endpoint"]),
        ("bi_fact_quote_latest", ["move_category"]),
        ("bi_fact_earnings", ["earnings_result", "report_hour"]),
        ("bi_fact_fundamental_latest", ["profitability_band", "leverage_band"]),
        ("bi_fact_pipeline_run", ["trigger_source", "duration_band", "status"]),
    ]
    _populate(pipeline)

    for view, columns in checks:
        for column in columns:
            nulls = pipeline.scalar(
                f"SELECT COUNT(*) FROM {view} WHERE {column} IS NULL")
            assert nulls == 0, f"{view}.{column} has {nulls} NULL(s)"


def test_quote_latest_is_one_row_per_symbol(pipeline):
    """The KPI cards read this; a second row would double them."""
    for _ in range(3):
        pipeline.finnhub.collect_quote("AAPL")
    assert pipeline.scalar("SELECT COUNT(*) FROM bi_fact_quote_latest") == 1


def test_quote_latest_is_the_newest_quote(pipeline):
    pipeline.finnhub.collect_quote("AAPL")
    pipeline.finnhub.collect_quote("AAPL")
    newest = pipeline.scalar("SELECT MAX(fetched_at_utc) FROM fact_market_quote")
    shown = pipeline.scalar("SELECT fetched_at_utc FROM bi_fact_quote_latest")
    assert shown == newest


def test_fundamental_latest_is_one_row_per_symbol(pipeline):
    for _ in range(3):
        pipeline.finnhub.collect_fundamentals("AAPL")
    assert pipeline.scalar("SELECT COUNT(*) FROM bi_fact_fundamental_latest") == 1


def test_indicator_pivot_has_no_duplicate_keys(pipeline):
    _populate(pipeline)
    rows = pipeline.scalar("SELECT COUNT(*) FROM bi_fact_indicator_daily")
    distinct = pipeline.scalar(
        "SELECT COUNT(*) FROM (SELECT DISTINCT symbol_id, date_key, interval_id "
        "FROM bi_fact_indicator_daily) x")
    assert rows == distinct


def test_api_call_success_flags_are_complementary(pipeline):
    _populate(pipeline)
    bad = pipeline.scalar(
        "SELECT COUNT(*) FROM bi_fact_api_call WHERE is_success + is_failure <> 1")
    assert bad == 0, "a call counted as both success and failure skews the rate"


def test_health_view_reflects_reality(pipeline):
    _populate(pipeline)
    symbols, calls, rate = pipeline.query(
        "SELECT symbols_tracked, api_calls_24h, success_rate_24h "
        "FROM bi_pipeline_health")[0]
    assert symbols == pipeline.scalar("SELECT COUNT(*) FROM dim_symbol")
    assert calls > 0
    assert 0 <= float(rate) <= 100


def test_health_view_survives_an_empty_database(pipeline):
    """
    A brand-new database must not make the monitor crash -- this is the
    first thing it queries right after migrate, before any data exists.
    """
    row = pipeline.query("SELECT symbols_tracked, quote_age_minutes, "
                         "success_rate_24h FROM bi_pipeline_health")
    assert len(row) == 1


def test_every_fact_date_key_joins_to_the_calendar(pipeline):
    """An unmatched date_key silently drops rows from every visual."""
    _populate(pipeline)
    for view in ("bi_fact_price_daily", "bi_fact_indicator_daily",
                 "bi_fact_api_call", "bi_fact_earnings",
                 "bi_fact_pipeline_run", "bi_fact_quote_latest"):
        orphans = pipeline.scalar(f"""
            SELECT COUNT(*) FROM {view} f
             WHERE NOT EXISTS (SELECT 1 FROM bi_dim_date d
                                WHERE d.date_key = f.date_key)
        """)
        assert orphans == 0, f"{view} has {orphans} rows the calendar cannot match"


def test_every_fact_symbol_joins_to_the_dimension(pipeline):
    _populate(pipeline)
    for view in ("bi_fact_price_daily", "bi_fact_indicator_daily",
                 "bi_fact_quote_latest", "bi_fact_fundamental_latest",
                 "bi_fact_earnings"):
        orphans = pipeline.scalar(f"""
            SELECT COUNT(*) FROM {view} f
             WHERE NOT EXISTS (SELECT 1 FROM bi_dim_symbol s
                                WHERE s.symbol_id = f.symbol_id)
        """)
        assert orphans == 0, f"{view} references an unknown symbol"


# ---------------------------------------------------------------------
# UPGRADING AN EXISTING DATABASE
#
# The tests that were missing when `migrate` failed against the live
# database while CI stayed green: CI builds a FRESH database every run, so
# every view is created from nothing and CREATE OR REPLACE VIEW never meets
# an older view to collide with. The live database always has one.
#
# Each of these runs the file on a connection of its own. The file is one
# transaction now, and a failed transaction leaves ITS connection unusable
# until rolled back -- borrowing the session connection would hand the next
# test a poisoned one.
# ---------------------------------------------------------------------
import pathlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import psycopg2  # noqa: E402

from tests.conftest import TEST_URL  # noqa: E402

VIEWS_SQL = (pathlib.Path(__file__).resolve().parent.parent
             / "sql/02_bi_views.sql").read_text()

BI_VIEW_COUNT = 16


def _apply_views_on_a_fresh_connection() -> None:
    conn = psycopg2.connect(TEST_URL, connect_timeout=10)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(VIEWS_SQL)
    finally:
        conn.close()


def _view_names(database) -> set[str]:
    with database.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.views "
                    "WHERE table_schema='public' AND table_name LIKE 'bi\\_%'")
        return {r[0] for r in cur.fetchall()}


def test_views_replace_an_older_version_with_different_columns(database):
    """
    The production failure, reproduced.

    CREATE OR REPLACE VIEW keeps the existing column list -- it may append
    one at the end and nothing more. A view left from before the _utc /
    _local split has different column names, so replacing it raises
    "cannot change name of view column", and every view further down the
    file is then never applied at all.
    """
    with database.cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS bi_fact_price_daily CASCADE")
        cur.execute("CREATE VIEW bi_fact_price_daily AS "
                    "SELECT 1 AS hour_utc, 2 AS something_else")

    _apply_views_on_a_fresh_connection()

    with database.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'bi_fact_price_daily'")
        columns = {r[0] for r in cur.fetchall()}

    assert "something_else" not in columns, "the stale view was not replaced"
    assert {"date_key", "trade_date", "close", "move_category"} <= columns


def test_an_outside_dependent_stops_the_migration_instead_of_being_dropped(database):
    """
    CASCADE would delete a view someone built in pgAdmin without a word.
    """
    _apply_views_on_a_fresh_connection()
    with database.cursor() as cur:
        cur.execute("CREATE VIEW my_own_report AS "
                    "SELECT * FROM bi_fact_quote_latest")
    try:
        with pytest.raises(psycopg2.errors.RaiseException) as caught:
            _apply_views_on_a_fresh_connection()
        assert "my_own_report" in str(caught.value)
        with database.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM my_own_report")
    finally:
        with database.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS my_own_report")

    _apply_views_on_a_fresh_connection()
    assert len(_view_names(database)) == BI_VIEW_COUNT


def test_views_can_be_applied_twice_in_a_row(database):
    """Every deploy re-runs this file; the second pass must be a no-op."""
    _apply_views_on_a_fresh_connection()
    first = _view_names(database)
    _apply_views_on_a_fresh_connection()
    assert _view_names(database) == first
    assert len(first) == BI_VIEW_COUNT



def test_a_migration_that_fails_halfway_changes_nothing(database, tmp_path):
    """
    Atomicity, run the way production runs it.

    The views are dropped near the top of this file and recreated over the
    next five hundred lines. A failure in between must not leave the
    database with the views already dropped and never rebuilt -- a deploy
    that merely FAILED would then take every Power BI report down with it.

    This one shells out to psql because psycopg2 would hide the answer:
    `with conn:` wraps whatever it is given in a transaction of its own, so
    the file looks atomic through psycopg2 whether or not it says BEGIN.
    psql commits each statement on its own, exactly as the Database
    workflow does, so only here does the file's own transaction matter.
    """
    psql = shutil.which("psql")
    if psql is None:
        pytest.skip("psql is not installed")

    _apply_views_on_a_fresh_connection()
    before = _view_names(database)
    assert len(before) == BI_VIEW_COUNT

    # Break the LAST view, so the drops and most of the creates have
    # already run by the time it fails.
    broken = VIEWS_SQL.replace(
        "CREATE OR REPLACE VIEW bi_storage_usage AS",
        "CREATE OR REPLACE VIEW bi_storage_usage AS SELECT no_such_column FROM pg_class; --",
        1,
    )
    assert broken != VIEWS_SQL, "the injection point moved; fix this test"

    script = tmp_path / "broken_views.sql"
    script.write_text(broken)
    done = subprocess.run(
        [psql, TEST_URL, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(script)],
        capture_output=True, text=True, timeout=120,
    )

    assert done.returncode != 0, "the injected error did not fail the script"
    assert "no_such_column" in done.stderr
    assert _view_names(database) == before, \
        "a failed migration left the database half-rebuilt"
    with database.cursor() as cur:
        for view in sorted(before):
            cur.execute(f"SELECT 1 FROM {view} LIMIT 1")
