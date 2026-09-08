"""Central configuration. Everything the pipeline needs comes from env vars."""

import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- database ---------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PGHOST = os.getenv("PGHOST", "")
PGPORT = _int("PGPORT", 5432)
PGDATABASE = os.getenv("PGDATABASE", "")
PGUSER = os.getenv("PGUSER", "")
PGPASSWORD = os.getenv("PGPASSWORD", "")
PGSSLMODE = os.getenv("PGSSLMODE", "require")

# --- api keys ---------------------------------------------------------
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

# --- what to collect --------------------------------------------------
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT,GOOGL").split(",") if s.strip()]

# Daily candles per symbol per fetch. Twelve Data allows up to 5000.
DAILY_OUTPUTSIZE = _int("DAILY_OUTPUTSIZE", 100)

# Intraday candles. Set INTRADAY_ENABLED=false to cut storage growth by ~90%.
INTRADAY_ENABLED = os.getenv("INTRADAY_ENABLED", "true").lower() not in ("false", "0", "no")
INTRADAY_INTERVAL = os.getenv("INTRADAY_INTERVAL", "5min")
INTRADAY_OUTPUTSIZE = _int("INTRADAY_OUTPUTSIZE", 30)

# Alpha Vantage rows kept per indicator per fetch.
INDICATOR_MAX_RECORDS = _int("INDICATOR_MAX_RECORDS", 30)

# --- pacing (respect provider rate limits) ----------------------------
SLEEP_FINNHUB = _int("SLEEP_FINNHUB", 1)
SLEEP_TWELVEDATA = _int("SLEEP_TWELVEDATA", 8)
SLEEP_ALPHAVANTAGE = _int("SLEEP_ALPHAVANTAGE", 15)

HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 30)

# --- run metadata -----------------------------------------------------
RUN_TRIGGER = os.getenv("RUN_TRIGGER", "manual")
RUN_UID = os.getenv("GITHUB_RUN_ID") or None
STATUS_FILE = os.getenv("STATUS_FILE", "status.json")


def missing_keys() -> list[str]:
    """Which API keys are absent. Used to fail fast with a clear message."""
    return [
        name
        for name, value in (
            ("FINNHUB_API_KEY", FINNHUB_API_KEY),
            ("ALPHAVANTAGE_API_KEY", ALPHAVANTAGE_API_KEY),
            ("TWELVEDATA_API_KEY", TWELVEDATA_API_KEY),
        )
        if not value
    ]


def has_database_config() -> bool:
    return bool(DATABASE_URL or (PGHOST and PGDATABASE and PGUSER))
