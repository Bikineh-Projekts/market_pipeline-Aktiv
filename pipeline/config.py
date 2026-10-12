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

# --- Databricks Lakebase OAuth (optional) -----------------------------
# Lakebase offers two ways to authenticate, and the difference matters for
# a scheduled pipeline:
#
#   native Postgres password role -- the password does not expire. This is
#       the right choice here and needs none of the settings below: put the
#       whole connection string in DATABASE_URL and you are done.
#
#   OAuth token -- the "password" is a token that dies after 60 minutes, so
#       a fixed DATABASE_URL secret stops working within the hour. Setting
#       the four variables below makes the pipeline mint a fresh token for
#       every connection and refresh it mid-run, which is the only way
#       OAuth survives a long backfill.
#
# All four must be present for OAuth to engage; otherwise DATABASE_URL is
# used exactly as given.
DATABRICKS_HOST = os.getenv("DATABRICKS_HOST", "").strip().rstrip("/")
DATABRICKS_CLIENT_ID = os.getenv("DATABRICKS_CLIENT_ID", "").strip()
DATABRICKS_CLIENT_SECRET = os.getenv("DATABRICKS_CLIENT_SECRET", "").strip()
# projects/<project-id>/branches/<branch-id>/endpoints/<endpoint-id>
LAKEBASE_ENDPOINT = os.getenv("LAKEBASE_ENDPOINT", "").strip()

# Refresh this many seconds before the token's stated expiry, so a query
# never starts with a credential that dies while it runs.
TOKEN_REFRESH_MARGIN = _int("TOKEN_REFRESH_MARGIN", 300)


def lakebase_oauth_enabled() -> bool:
    return all((DATABRICKS_HOST, DATABRICKS_CLIENT_ID,
                DATABRICKS_CLIENT_SECRET, LAKEBASE_ENDPOINT))


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

# Retries for transient failures only (timeouts, connection errors, 5xx).
# Quota and 4xx responses are never retried.
HTTP_RETRIES = _int("HTTP_RETRIES", 2)
HTTP_BACKOFF = float(os.getenv("HTTP_BACKOFF", "1.5"))

# --- run metadata -----------------------------------------------------
RUN_TRIGGER = os.getenv("RUN_TRIGGER", "manual")

# log_pipeline_run.run_uid is UNIQUE and the insert is ON CONFLICT DO UPDATE,
# so a repeated uid silently overwrites an earlier run's row instead of adding
# one. GITHUB_RUN_ID stays the same when you re-run a workflow, which would
# erase the failed attempt you were trying to diagnose -- GITHUB_RUN_ATTEMPT
# is what distinguishes them.
_gh_run = os.getenv("GITHUB_RUN_ID")
if _gh_run:
    RUN_UID = f"gha-{_gh_run}-{os.getenv('GITHUB_RUN_ATTEMPT', '1')}"
else:
    RUN_UID = None

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
