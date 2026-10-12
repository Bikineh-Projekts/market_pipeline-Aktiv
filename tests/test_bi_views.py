"""
The Power BI layer.

These views are the project's actual product: if a category column turns
to NULL or a derived number goes wrong, the report is wrong even though
every row loaded fine.
"""

from __future__ import annotations

from decimal import Decimal


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
