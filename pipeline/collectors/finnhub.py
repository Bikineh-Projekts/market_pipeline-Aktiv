"""Finnhub → fact_market_quote, fact_company_fundamental, fact_earnings_calendar."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from psycopg2.extras import Json

from .. import config, db
from .base import ApiError, fetch, pause

logger = logging.getLogger(__name__)

SOURCE = "finnhub"
BASE = "https://finnhub.io/api/v1"


def _headers() -> dict:
    return {"X-Finnhub-Token": config.FINNHUB_API_KEY}


def _num(value):
    """Finnhub returns 0 for 'no data' on some fields; keep real zeros only."""
    return value if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------------
def collect_quote(symbol: str, run_id: int | None = None) -> int:
    data = fetch(SOURCE, f"{BASE}/quote", "/quote",
                 params={"symbol": symbol}, headers=_headers(),
                 symbol=symbol, run_id=run_id)

    price = _num(data.get("c"))
    prev = _num(data.get("pc"))

    if not price:
        raise ApiError(f"finnhub returned no price for {symbol}")

    change = round(price - prev, 6) if prev else None
    change_pct = round((price - prev) / prev * 100, 4) if prev else None
    quote_time = datetime.fromtimestamp(data["t"], tz=timezone.utc) if data.get("t") else None

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_market_quote
                (symbol_id, source_id, fetched_at_utc, quote_time_utc,
                 price, open, high, low, previous_close, change, change_pct, raw_payload)
            VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol_id, source_id, quote_time_utc) DO UPDATE SET
                price          = EXCLUDED.price,
                open           = EXCLUDED.open,
                high           = EXCLUDED.high,
                low            = EXCLUDED.low,
                previous_close = EXCLUDED.previous_close,
                change         = EXCLUDED.change,
                change_pct     = EXCLUDED.change_pct,
                raw_payload    = EXCLUDED.raw_payload,
                fetched_at_utc = NOW()
            """,
            (
                db.symbol_id(symbol), db.source_id(SOURCE), quote_time,
                price, _num(data.get("o")), _num(data.get("h")), _num(data.get("l")),
                prev, change, change_pct, Json(data),
            ),
        )

    print(f"    finnhub quote     {symbol}: {price}")
    return 1


# ---------------------------------------------------------------------
def collect_fundamentals(symbol: str, run_id: int | None = None) -> int:
    profile = fetch(SOURCE, f"{BASE}/stock/profile2", "/stock/profile2",
                    params={"symbol": symbol}, headers=_headers(),
                    symbol=symbol, run_id=run_id)
    pause(config.SLEEP_FINNHUB)

    metrics_raw = fetch(SOURCE, f"{BASE}/stock/metric", "/stock/metric",
                        params={"symbol": symbol, "metric": "all"}, headers=_headers(),
                        symbol=symbol, run_id=run_id)

    m = metrics_raw.get("metric", {}) or {}

    ipo = None
    if profile.get("ipo"):
        try:
            ipo = datetime.strptime(profile["ipo"], "%Y-%m-%d").date()
        except ValueError:
            pass

    sym_id = db.symbol_id(
        symbol,
        company_name=profile.get("name"),
        exchange=profile.get("exchange"),
        country=profile.get("country"),
        currency=profile.get("currency"),
        industry=profile.get("finnhubIndustry"),
    )

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_company_fundamental
                (symbol_id, source_id, fetched_at_utc, fetched_date, ipo_date,
                 market_cap, share_outstanding, pe_ratio, eps_ttm,
                 gross_margin, net_margin, roe, debt_to_equity, current_ratio,
                 beta, week_52_high, week_52_low, raw_profile, raw_metrics)
            VALUES (%s, %s, NOW(), CURRENT_DATE, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol_id, source_id, fetched_date) DO UPDATE SET
                market_cap        = EXCLUDED.market_cap,
                share_outstanding = EXCLUDED.share_outstanding,
                pe_ratio          = EXCLUDED.pe_ratio,
                eps_ttm           = EXCLUDED.eps_ttm,
                gross_margin      = EXCLUDED.gross_margin,
                net_margin        = EXCLUDED.net_margin,
                roe               = EXCLUDED.roe,
                debt_to_equity    = EXCLUDED.debt_to_equity,
                current_ratio     = EXCLUDED.current_ratio,
                beta              = EXCLUDED.beta,
                week_52_high      = EXCLUDED.week_52_high,
                week_52_low       = EXCLUDED.week_52_low,
                raw_profile       = EXCLUDED.raw_profile,
                raw_metrics       = EXCLUDED.raw_metrics,
                fetched_at_utc    = NOW()
            """,
            (
                sym_id, db.source_id(SOURCE), ipo,
                profile.get("marketCapitalization"),
                profile.get("shareOutstanding"),
                m.get("peNormalizedAnnual"),
                m.get("epsNormalizedAnnual"),
                m.get("grossMarginAnnual"),
                m.get("netProfitMarginAnnual"),
                m.get("roeAnnual"),
                m.get("totalDebt/totalEquityAnnual"),
                m.get("currentRatioAnnual"),
                m.get("beta"),
                m.get("52WeekHigh"),
                m.get("52WeekLow"),
                Json(profile), Json(metrics_raw),
            ),
        )

    print(f"    finnhub profile   {symbol}: {profile.get('name') or 'unknown'}")
    return 1


# ---------------------------------------------------------------------
def collect_earnings(symbol: str, run_id: int | None = None) -> int:
    today = date.today()
    data = fetch(
        SOURCE, f"{BASE}/calendar/earnings", "/calendar/earnings",
        params={
            "symbol": symbol,
            "from": today.strftime("%Y-%m-%d"),
            "to": (today + timedelta(days=180)).strftime("%Y-%m-%d"),
        },
        headers=_headers(), symbol=symbol, run_id=run_id,
    )

    items = data.get("earningsCalendar") or []
    if not items:
        print(f"    finnhub earnings  {symbol}: none scheduled")
        return 0

    sym_id = db.symbol_id(symbol)
    src_id = db.source_id(SOURCE)
    written = 0

    with db.cursor() as cur:
        for item in items:
            report_date = None
            if item.get("date"):
                try:
                    report_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
                except ValueError:
                    continue
            if report_date is None:
                continue

            cur.execute(
                """
                INSERT INTO fact_earnings_calendar
                    (symbol_id, source_id, fetched_at_utc, report_date, hour,
                     eps_estimate, eps_actual, revenue_estimate, revenue_actual, raw_payload)
                VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol_id, report_date) DO UPDATE SET
                    eps_estimate     = EXCLUDED.eps_estimate,
                    eps_actual       = EXCLUDED.eps_actual,
                    revenue_estimate = EXCLUDED.revenue_estimate,
                    revenue_actual   = EXCLUDED.revenue_actual,
                    raw_payload      = EXCLUDED.raw_payload,
                    fetched_at_utc   = NOW()
                """,
                (
                    sym_id, src_id, report_date, item.get("hour"),
                    item.get("epsEstimate"), item.get("epsActual"),
                    item.get("revenueEstimate"), item.get("revenueActual"),
                    Json(item),
                ),
            )
            written += 1

    print(f"    finnhub earnings  {symbol}: {written} rows")
    return written


# ---------------------------------------------------------------------
def run(symbol: str, with_fundamentals: bool = False, with_earnings: bool = False,
        run_id: int | None = None) -> int:
    """Quote always; the slower endpoints only when asked."""
    rows = collect_quote(symbol, run_id)

    if with_fundamentals:
        pause(config.SLEEP_FINNHUB)
        rows += collect_fundamentals(symbol, run_id)

    if with_earnings:
        pause(config.SLEEP_FINNHUB)
        rows += collect_earnings(symbol, run_id)

    return rows
