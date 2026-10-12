"""Collector behaviour: parsing, upserting, logging, retries, quotas."""

from __future__ import annotations

import pytest

from pipeline.collectors.base import ApiError, RateLimited
from tests import mock_provider


# ---------------------------------------------------------------- finnhub
def test_quote_is_stored_with_derived_change(pipeline):
    pipeline.finnhub.collect_quote("AAPL")

    row = pipeline.query("""
        SELECT price, previous_close, change, change_pct
          FROM fact_market_quote WHERE symbol_id = (
              SELECT symbol_id FROM dim_symbol WHERE symbol_code='AAPL')
    """)[0]
    price, prev, change, change_pct = (float(v) for v in row)

    assert price > 0
    assert change == pytest.approx(price - prev, abs=1e-4)
    assert change_pct == pytest.approx((price - prev) / prev * 100, abs=1e-3)


def test_quote_with_no_price_is_an_error_not_a_zero_row(pipeline):
    """A provider returning zeros must not land a bogus price of 0."""
    mock_provider.FAULTS["finnhub_empty_quote"] = True
    with pytest.raises(ApiError):
        pipeline.finnhub.collect_quote("AAPL")
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_market_quote") == 0


def test_fundamentals_keep_one_row_per_day(pipeline):
    """Without the unique constraint this grew by a row every 30 minutes."""
    for _ in range(3):
        pipeline.finnhub.collect_fundamentals("AAPL")
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_company_fundamental") == 1


def test_fundamentals_populate_the_symbol_profile(pipeline):
    pipeline.finnhub.collect_quote("AAPL")
    pipeline.finnhub.collect_fundamentals("AAPL")

    row = pipeline.query(
        "SELECT company_name, country, sector FROM dim_symbol WHERE symbol_code='AAPL'"
    )[0]
    assert row[0] == "Apple Inc"
    assert row[1] == "US"
    assert row[2] == "Technology"


def test_earnings_upsert_does_not_duplicate(pipeline):
    first = pipeline.finnhub.collect_earnings("AAPL")
    pipeline.finnhub.collect_earnings("AAPL")
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_earnings_calendar") == first


def test_earnings_classify_beat_and_upcoming(pipeline):
    pipeline.finnhub.collect_earnings("AAPL")
    results = dict(pipeline.query(
        "SELECT earnings_result, is_upcoming FROM bi_fact_earnings"))
    assert "Beat" in results           # the past report beat its estimate
    assert "Upcoming" in results       # the future one has no actual yet


# ------------------------------------------------------------- twelvedata
def test_daily_candles_are_stored(pipeline):
    written = pipeline.twelvedata.collect_interval("AAPL", "1day", 30)
    assert written == 30
    assert pipeline.scalar("""
        SELECT COUNT(*) FROM fact_market_timeseries t
          JOIN dim_interval i USING (interval_id)
         WHERE i.interval_code = '1day'
    """) == 30


def test_candles_upsert_rather_than_duplicate(pipeline):
    pipeline.twelvedata.collect_interval("AAPL", "1day", 30)
    pipeline.twelvedata.collect_interval("AAPL", "1day", 30)
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_market_timeseries") == 30


def test_provider_error_in_a_200_body_is_detected(pipeline):
    """Twelve Data reports failure inside an HTTP 200 response."""
    mock_provider.FAULTS["twelvedata_error"] = True
    with pytest.raises(ApiError):
        pipeline.twelvedata.collect_interval("AAPL", "1day", 30)


def test_intraday_can_be_switched_off(pipeline, monkeypatch):
    """The storage lever: INTRADAY_ENABLED=false must fetch daily only."""
    monkeypatch.setattr(pipeline.config, "INTRADAY_ENABLED", False)
    pipeline.twelvedata.run("AAPL")

    intraday = pipeline.scalar("""
        SELECT COUNT(*) FROM fact_market_timeseries t
          JOIN dim_interval i USING (interval_id)
         WHERE i.interval_type = 'intraday'
    """)
    assert intraday == 0
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_market_timeseries") > 0


def test_intraday_failure_does_not_lose_the_daily_candles(pipeline, monkeypatch):
    """Intraday is a bonus; it must not take the day's data down with it."""
    monkeypatch.setattr(pipeline.config, "INTRADAY_ENABLED", True)
    monkeypatch.setattr(pipeline.config, "INTRADAY_INTERVAL", "5min")
    rows = pipeline.twelvedata.run("AAPL")
    assert rows > 0


# ----------------------------------------------------------- alphavantage
def test_indicators_are_pivoted_into_one_row_per_day(pipeline):
    for name in ("RSI", "MACD", "EMA", "SMA"):
        pipeline.alphavantage.collect_indicator("AAPL", name, max_records=5)

    rows = pipeline.query("""
        SELECT rsi_14, ema_20, sma_50, macd, macd_signal, macd_hist
          FROM bi_fact_indicator_daily ORDER BY date_key DESC LIMIT 1
    """)
    assert rows, "the pivot view returned nothing"
    assert all(v is not None for v in rows[0]), (
        "every indicator should land on the same pivoted row")


def test_indicator_signals_are_classified(pipeline):
    for name in ("RSI", "MACD", "EMA", "SMA"):
        pipeline.alphavantage.collect_indicator("AAPL", name, max_records=5)

    signal, trend = pipeline.query(
        "SELECT rsi_signal, trend_category FROM bi_fact_indicator_daily LIMIT 1")[0]
    assert signal in {"Overbought", "Oversold", "Neutral"}
    assert trend in {"Golden Cross Zone", "Death Cross Zone"}


def test_quota_message_raises_rate_limited(pipeline):
    """A 200 OK carrying the quota text must not look like success."""
    mock_provider.FAULTS["alphavantage_quota"] = True
    with pytest.raises(RateLimited):
        pipeline.alphavantage.collect_indicator("AAPL", "RSI")


def test_quota_stops_the_remaining_indicators(pipeline):
    """Spending more of a spent quota is pure waste."""
    mock_provider.FAULTS["alphavantage_quota"] = True
    mock_provider.CALLS.clear()
    with pytest.raises(RateLimited):
        pipeline.alphavantage.run("AAPL")
    assert mock_provider.CALLS.get("/av/query", 0) == 1, (
        "should stop after the first quota refusal, not try all four")


# ------------------------------------------------------- logging, retries
def test_every_call_is_logged_including_failures(pipeline):
    """
    The original bug: logging happened only after success, so the health
    monitor's success rate was always 100% even while everything failed.
    """
    pipeline.finnhub.collect_quote("AAPL")
    assert pipeline.scalar(
        "SELECT COUNT(*) FROM log_api_call WHERE http_status = 200") >= 1

    mock_provider.FAULTS["finnhub_status"] = 404
    with pytest.raises(ApiError):
        pipeline.finnhub.collect_quote("AAPL")

    assert pipeline.scalar(
        "SELECT COUNT(*) FROM log_api_call WHERE http_status = 404") == 1


def test_latency_is_recorded(pipeline):
    pipeline.finnhub.collect_quote("AAPL")
    ms = pipeline.scalar("SELECT response_ms FROM log_api_call LIMIT 1")
    assert ms is not None and ms >= 0


def test_retries_are_on_by_default(pipeline):
    """
    Asserted on the shipped default, not a monkeypatched value: a test that
    sets HTTP_RETRIES itself would still pass if the default were 0, and the
    deployed pipeline would have no retries at all.
    """
    assert pipeline.config.HTTP_RETRIES >= 1


def test_transient_failure_is_retried_and_succeeds(pipeline, monkeypatch):
    """
    Two bad seconds from a provider should not cost the symbol its data.
    Only the backoff is patched, so this exercises the real retry count.
    """
    monkeypatch.setattr(pipeline.config, "HTTP_BACKOFF", 0)
    mock_provider.FAULTS["fail_first_n"] = pipeline.config.HTTP_RETRIES

    pipeline.finnhub.collect_quote("AAPL")  # must not raise

    assert pipeline.scalar("SELECT COUNT(*) FROM fact_market_quote") == 1
    assert pipeline.scalar(
        "SELECT COUNT(*) FROM log_api_call WHERE http_status = 503"
    ) == pipeline.config.HTTP_RETRIES, "each attempt should appear in the log"


def test_retries_are_bounded(pipeline, monkeypatch):
    """A permanently broken provider must not retry forever."""
    monkeypatch.setattr(pipeline.config, "HTTP_BACKOFF", 0)
    mock_provider.FAULTS["fail_first_n"] = 99

    with pytest.raises(ApiError):
        pipeline.finnhub.collect_quote("AAPL")

    attempts = pipeline.scalar("SELECT COUNT(*) FROM log_api_call")
    expected = pipeline.config.HTTP_RETRIES + 1
    assert attempts == expected, f"expected {expected} attempts, got {attempts}"


def test_client_errors_are_not_retried(pipeline, monkeypatch):
    """A 404 will stay a 404; retrying only burns quota."""
    monkeypatch.setattr(pipeline.config, "HTTP_RETRIES", 3)
    monkeypatch.setattr(pipeline.config, "HTTP_BACKOFF", 0)
    mock_provider.FAULTS["finnhub_status"] = 404

    with pytest.raises(ApiError):
        pipeline.finnhub.collect_quote("AAPL")

    assert pipeline.scalar("SELECT COUNT(*) FROM log_api_call") == 1


def test_quota_is_not_retried(pipeline, monkeypatch):
    monkeypatch.setattr(pipeline.config, "HTTP_RETRIES", 3)
    monkeypatch.setattr(pipeline.config, "HTTP_BACKOFF", 0)
    mock_provider.FAULTS["alphavantage_quota"] = True

    with pytest.raises(RateLimited):
        pipeline.alphavantage.collect_indicator("AAPL", "RSI")

    assert pipeline.scalar("SELECT COUNT(*) FROM log_api_call") == 1
