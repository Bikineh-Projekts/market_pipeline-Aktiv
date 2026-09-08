"""Twelve Data → fact_market_timeseries (OHLCV candles)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg2.extras import Json, execute_batch

from .. import config, db
from .base import ApiError, fetch, pause

logger = logging.getLogger(__name__)

SOURCE = "twelvedata"
BASE = "https://api.twelvedata.com"


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


INSERT_SQL = """
INSERT INTO fact_market_timeseries
    (symbol_id, source_id, interval_id, candle_time_utc,
     open, high, low, close, volume, raw_payload)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol_id, interval_id, candle_time_utc) DO UPDATE SET
    open        = EXCLUDED.open,
    high        = EXCLUDED.high,
    low         = EXCLUDED.low,
    close       = EXCLUDED.close,
    volume      = EXCLUDED.volume,
    raw_payload = EXCLUDED.raw_payload
"""


def collect_interval(symbol: str, interval: str, outputsize: int,
                     run_id: int | None = None) -> int:
    data = fetch(
        SOURCE, f"{BASE}/time_series", "/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC",
            "apikey": config.TWELVEDATA_API_KEY,
        },
        symbol=symbol, run_id=run_id,
    )

    values = data.get("values") or []
    if not values:
        raise ApiError(f"twelvedata returned no candles for {symbol} {interval}")

    sym_id = db.symbol_id(symbol)
    src_id = db.source_id(SOURCE)
    int_id = db.interval_id(interval)

    rows = []
    for item in values:
        candle_time = _parse_dt(item.get("datetime", ""))
        if candle_time is None:
            continue
        rows.append((
            sym_id, src_id, int_id, candle_time,
            _float(item.get("open")), _float(item.get("high")),
            _float(item.get("low")), _float(item.get("close")),
            _float(item.get("volume")), Json(item),
        ))

    if not rows:
        raise ApiError(f"twelvedata: no parseable candles for {symbol} {interval}")

    # execute_batch is far fewer round trips than a loop of execute(),
    # which matters on a remote database.
    with db.cursor() as cur:
        execute_batch(cur, INSERT_SQL, rows, page_size=200)

    print(f"    twelvedata {interval:<5} {symbol}: {len(rows)} candles")
    return len(rows)


def run(symbol: str, daily_size: int | None = None, run_id: int | None = None) -> int:
    """
    Daily candles always. Intraday only when INTRADAY_ENABLED is on —
    intraday is roughly 90% of this project's storage growth.
    """
    daily_size = daily_size or config.DAILY_OUTPUTSIZE

    rows = collect_interval(symbol, "1day", daily_size, run_id)

    if config.INTRADAY_ENABLED:
        pause(config.SLEEP_TWELVEDATA, "twelvedata rate limit")
        try:
            rows += collect_interval(
                symbol, config.INTRADAY_INTERVAL, config.INTRADAY_OUTPUTSIZE, run_id
            )
        except ApiError as exc:
            # Intraday is optional data; do not fail the symbol over it.
            logger.warning("intraday failed for %s: %s", symbol, exc)
            print(f"    twelvedata intra  {symbol}: skipped ({exc})")

    return rows
