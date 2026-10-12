"""
A stand-in for Finnhub, Twelve Data and Alpha Vantage.

Runs on localhost so the suite never touches a real provider: no API keys,
no quota spent, and failures can be provoked on demand. `FAULTS` is the
switchboard the tests use to make a provider misbehave.
"""

from __future__ import annotations

import json
import random
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# Flip these from a test to provoke a specific failure.
FAULTS: dict[str, object] = {
    "finnhub_status": None,      # e.g. 500 -> /quote returns that status
    "finnhub_empty_quote": False,  # 200 OK but no price in the body
    "twelvedata_error": False,   # 200 OK with {"status": "error"}
    "alphavantage_quota": False,  # 200 OK with the quota message
    "fail_first_n": 0,           # first N calls to any endpoint return 503
}

CALLS: dict[str, int] = {}


def reset() -> None:
    """Back to a healthy provider with no call history."""
    FAULTS.update({
        "finnhub_status": None,
        "finnhub_empty_quote": False,
        "twelvedata_error": False,
        "alphavantage_quota": False,
        "fail_first_n": 0,
    })
    CALLS.clear()


# ---------------------------------------------------------------- payloads
_PRICES = {"AAPL": 220.0, "MSFT": 430.0, "GOOGL": 175.0, "TSLA": 250.0}


def _quote(symbol: str) -> dict:
    if FAULTS["finnhub_empty_quote"]:
        return {"c": 0, "o": 0, "h": 0, "l": 0, "pc": 0, "t": 0}
    base = _PRICES.get(symbol, 100.0)
    price = round(base + random.uniform(-3, 3), 2)
    return {
        "c": price, "o": round(price - 1, 2), "h": round(price + 2, 2),
        "l": round(price - 2, 2), "pc": round(price - 0.8, 2),
        "t": int(datetime.now(timezone.utc).timestamp()),
    }


def _profile(symbol: str) -> dict:
    names = {"AAPL": "Apple Inc", "MSFT": "Microsoft Corp",
             "GOOGL": "Alphabet Inc", "TSLA": "Tesla Inc"}
    return {
        "name": names.get(symbol, symbol),
        "exchange": "NASDAQ NMS - GLOBAL MARKET",
        "country": "US", "currency": "USD", "ipo": "1980-12-12",
        "marketCapitalization": 3_400_000.0,
        "shareOutstanding": 15_300.0,
        "finnhubIndustry": "Technology",
    }


def _metrics() -> dict:
    return {"metric": {
        "peNormalizedAnnual": 32.4, "epsNormalizedAnnual": 6.75,
        "grossMarginAnnual": 45.2, "netProfitMarginAnnual": 26.3,
        "roeAnnual": 147.9, "totalDebt/totalEquityAnnual": 187.0,
        "currentRatioAnnual": 0.95, "beta": 1.22,
        "52WeekHigh": 260.1, "52WeekLow": 164.0,
    }}


def _earnings(symbol: str) -> dict:
    today = date.today()
    return {"earningsCalendar": [
        {"date": (today + timedelta(days=21)).isoformat(), "hour": "amc",
         "epsEstimate": 2.35, "epsActual": None,
         "revenueEstimate": 94_000_000_000, "revenueActual": None, "symbol": symbol},
        {"date": (today - timedelta(days=70)).isoformat(), "hour": "amc",
         "epsEstimate": 1.90, "epsActual": 2.05,
         "revenueEstimate": 88_000_000_000, "revenueActual": 91_000_000_000,
         "symbol": symbol},
    ]}


def _series(symbol: str, interval: str, outputsize) -> dict:
    if FAULTS["twelvedata_error"]:
        return {"status": "error", "message": "**symbol** not found"}

    values, price = [], 200.0
    step = timedelta(days=1) if interval == "1day" else timedelta(minutes=5)
    fmt = "%Y-%m-%d" if interval == "1day" else "%Y-%m-%d %H:%M:%S"
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    for i in range(int(outputsize)):
        price += random.uniform(-2, 2)
        stamp = now - step * i
        values.append({
            "datetime": stamp.strftime(fmt),
            "open": f"{price:.2f}", "high": f"{price + 1.5:.2f}",
            "low": f"{price - 1.5:.2f}", "close": f"{price + 0.3:.2f}",
            "volume": str(random.randint(1_000_000, 9_000_000)),
        })
    return {"meta": {"symbol": symbol, "interval": interval},
            "values": values, "status": "ok"}


def _indicator(function: str, rows: int = 30) -> dict:
    if FAULTS["alphavantage_quota"]:
        return {"Information": "our standard API rate limit is 25 requests per day"}

    key = f"Technical Analysis: {function}"
    series, today = {}, datetime.now(timezone.utc).date()
    for i in range(rows):
        day = (today - timedelta(days=i)).isoformat()
        if function == "MACD":
            series[day] = {
                "MACD": f"{random.uniform(-2, 2):.4f}",
                "MACD_Signal": f"{random.uniform(-2, 2):.4f}",
                "MACD_Hist": f"{random.uniform(-1, 1):.4f}",
            }
        else:
            series[day] = {function: f"{random.uniform(20, 80):.4f}"}
    return {"Meta Data": {"1: Symbol": "TEST"}, key: series}


# ---------------------------------------------------------------- server
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep pytest output clean

    def _send(self, code: int, body) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path
        symbol = query.get("symbol", "TEST")

        CALLS[path] = CALLS.get(path, 0) + 1

        # Transient-failure injection, for the retry tests.
        if FAULTS["fail_first_n"]:
            FAULTS["fail_first_n"] = int(FAULTS["fail_first_n"]) - 1
            return self._send(503, {"error": "service unavailable"})

        if path == "/fh/quote":
            status = FAULTS["finnhub_status"]
            if status:
                return self._send(int(status), {"error": "injected"})
            return self._send(200, _quote(symbol))
        if path == "/fh/stock/profile2":
            return self._send(200, _profile(symbol))
        if path == "/fh/stock/metric":
            return self._send(200, _metrics())
        if path == "/fh/calendar/earnings":
            return self._send(200, _earnings(symbol))

        if path == "/td/time_series":
            return self._send(200, _series(
                symbol, query.get("interval", "1day"), query.get("outputsize", 30)))

        if path == "/av/query":
            return self._send(200, _indicator(query.get("function", "RSI")))

        self._send(404, {"error": "not found"})


class MockProvider:
    """Context manager that serves the fake endpoints on a free port."""

    def __init__(self, port: int = 0):
        self._server = HTTPServer(("127.0.0.1", port), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def finnhub_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/fh"

    @property
    def twelvedata_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/td"

    @property
    def alphavantage_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/av/query"

    def __enter__(self) -> "MockProvider":
        self._thread.start()
        reset()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
