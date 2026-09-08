"""
Alpha Vantage → fact_market_indicator (RSI, MACD, EMA, SMA).

The free tier allows 25 requests per day in total. Four indicators per
symbol means three symbols consume half the daily budget in one run, so
this collector is only invoked by the nightly job, never every 30 minutes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg2.extras import Json, execute_batch

from .. import config, db
from .base import ApiError, fetch, pause

logger = logging.getLogger(__name__)

SOURCE = "alphavantage"
BASE = "https://www.alphavantage.co/query"

# name -> (function, extra params, value field in the response)
INDICATORS = {
    "RSI":  ({"function": "RSI",  "time_period": 14, "series_type": "close"}, "RSI"),
    "MACD": ({"function": "MACD", "series_type": "close",
              "fastperiod": 12, "slowperiod": 26, "signalperiod": 9}, "MACD"),
    "EMA":  ({"function": "EMA",  "time_period": 20, "series_type": "close"}, "EMA"),
    "SMA":  ({"function": "SMA",  "time_period": 50, "series_type": "close"}, "SMA"),
}

INSERT_SQL = """
INSERT INTO fact_market_indicator
    (symbol_id, source_id, indicator_id, interval_id, candle_time_utc,
     value, macd, macd_signal, macd_hist, raw_payload)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol_id, indicator_id, interval_id, candle_time_utc) DO UPDATE SET
    value       = EXCLUDED.value,
    macd        = EXCLUDED.macd,
    macd_signal = EXCLUDED.macd_signal,
    macd_hist   = EXCLUDED.macd_hist,
    raw_payload = EXCLUDED.raw_payload
"""


def _parse_dt(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _series_key(payload: dict, indicator: str) -> str | None:
    """Alpha Vantage names the data block 'Technical Analysis: RSI' etc."""
    return next((k for k in payload if "Technical Analysis" in k and indicator in k), None)


def collect_indicator(symbol: str, indicator: str, interval: str = "daily",
                      max_records: int | None = None, run_id: int | None = None) -> int:
    params, value_field = INDICATORS[indicator]
    max_records = max_records or config.INDICATOR_MAX_RECORDS

    payload = fetch(
        SOURCE, BASE, f"/query?function={params['function']}",
        params={**params, "symbol": symbol, "interval": interval,
                "apikey": config.ALPHAVANTAGE_API_KEY},
        symbol=symbol, run_id=run_id,
    )

    key = _series_key(payload, indicator)
    if not key:
        raise ApiError(f"alphavantage: no {indicator} series for {symbol}")

    sym_id = db.symbol_id(symbol)
    src_id = db.source_id(SOURCE)
    ind_id = db.indicator_id(indicator)
    int_id = db.interval_id(interval)

    rows = []
    for dt_str, values in list(payload[key].items())[:max_records]:
        candle_time = _parse_dt(dt_str)
        if candle_time is None:
            continue

        if indicator == "MACD":
            macd = _float(values.get("MACD"))
            rows.append((
                sym_id, src_id, ind_id, int_id, candle_time,
                macd, macd, _float(values.get("MACD_Signal")),
                _float(values.get("MACD_Hist")), Json({dt_str: values}),
            ))
        else:
            rows.append((
                sym_id, src_id, ind_id, int_id, candle_time,
                _float(values.get(value_field)), None, None, None,
                Json({dt_str: values}),
            ))

    if not rows:
        raise ApiError(f"alphavantage: {indicator} for {symbol} had no usable rows")

    with db.cursor() as cur:
        execute_batch(cur, INSERT_SQL, rows, page_size=200)

    print(f"    alphavantage {indicator:<4} {symbol}: {len(rows)} rows")
    return len(rows)


def run(symbol: str, interval: str = "daily", run_id: int | None = None) -> int:
    """
    Collect all four indicators. A failure on one indicator (usually the
    daily quota) stops the rest for this symbol — continuing would just
    burn requests that are guaranteed to fail too.
    """
    rows = 0

    for index, indicator in enumerate(INDICATORS):
        try:
            rows += collect_indicator(symbol, indicator, interval, run_id=run_id)
        except ApiError as exc:
            message = str(exc).lower()
            if "rate limit" in message or "frequency" in message or "25 requests" in message:
                print(f"    alphavantage quota reached, skipping remaining indicators")
                raise
            logger.warning("%s failed for %s: %s", indicator, symbol, exc)
            print(f"    alphavantage {indicator:<4} {symbol}: failed ({exc})")

        if index < len(INDICATORS) - 1:
            pause(config.SLEEP_ALPHAVANTAGE, "alphavantage rate limit")

    return rows
