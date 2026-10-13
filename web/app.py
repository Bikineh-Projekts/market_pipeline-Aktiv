"""
Read-only status dashboard for the market pipeline.

Deployed on Render as a web service. It does NOT run the ETL and does NOT
create the schema -- GitHub Actions does both. This process only reads the
bi_* views, in read-only transactions enforced by Postgres.

Local:   python -m web.app
Render:  gunicorn 'web.app:create_app()' --config web/gunicorn.conf.py
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template

from . import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ queries
HEALTH_SQL = "SELECT * FROM bi_pipeline_health"

LAST_RUNS_SQL = """
    SELECT run_uid, trigger_source, status,
           started_at_local, finished_at_local,
           duration_seconds, symbols_ok, symbols_failed, rows_written,
           NULLIF(error_msg, '') AS error_msg
      FROM bi_fact_pipeline_run
     ORDER BY started_at_utc DESC
     LIMIT 10
"""

QUOTES_SQL = """
    SELECT s.symbol_code, s.company_name, s.sector, s.market_cap_band,
           q.price, q.change, q.change_pct, q.move_category,
           q.fetched_at_local, ROUND(q.minutes_since_update) AS age_minutes
      FROM bi_fact_quote_latest q
      JOIN bi_dim_symbol s USING (symbol_id)
     ORDER BY s.symbol_code
"""

SOURCES_SQL = """
    SELECT src.source_label AS source,
           COUNT(*)                                   AS calls,
           SUM(c.is_success)                          AS ok,
           SUM(c.is_failure)                          AS failed,
           ROUND(AVG(c.response_ms))                  AS avg_ms,
           MAX(c.called_at_local)                     AS last_call
      FROM bi_fact_api_call c
      JOIN bi_dim_source src USING (source_id)
     WHERE c.called_at_utc > NOW() - INTERVAL '24 hours'
     GROUP BY src.source_label
     ORDER BY calls DESC
"""

ERRORS_SQL = """
    SELECT c.called_at_local, src.source_label AS source, c.endpoint,
           c.http_status, c.status_category, LEFT(c.error_msg, 120) AS error_msg
      FROM bi_fact_api_call c
      LEFT JOIN bi_dim_source src USING (source_id)
     WHERE c.is_failure = 1
       AND c.called_at_utc > NOW() - INTERVAL '48 hours'
     ORDER BY c.called_at_utc DESC
     LIMIT 15
"""

COVERAGE_SQL = """
    SELECT s.symbol_code,
           COUNT(p.date_key)        AS daily_candles,
           MIN(p.trade_date)        AS first_day,
           MAX(p.trade_date)        AS last_day
      FROM bi_dim_symbol s
      LEFT JOIN bi_fact_price_daily p USING (symbol_id)
     GROUP BY s.symbol_code
     ORDER BY s.symbol_code
"""


# ------------------------------------------------------------------ helpers
def _display_zone() -> ZoneInfo:
    """
    The zone the page reads in.

    Falls back to UTC rather than raising: a mistyped APP_TIMEZONE should
    cost a wrong label, not a 500 on every request.
    """
    try:
        return ZoneInfo(db.app_timezone())
    except Exception:  # unknown zone name, or no tzdata in the image
        logger.warning("unknown APP_TIMEZONE %r, showing UTC", db.app_timezone())
        return timezone.utc


def _timezone_label() -> str:
    """
    "CET" or "CEST", whichever applies today -- not a fixed string.

    Berlin is +01:00 for part of the year and +02:00 for the rest, so a
    hardcoded label is wrong for about seven months of it.
    """
    return datetime.now(_display_zone()).strftime("%Z") or "UTC"


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _clean(rows: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in row.items()} for row in rows]


def _overall_state(health: dict | None, last_run: dict | None) -> tuple[str, str]:
    """
    A single word for the banner, plus why.

    "Not started" is kept separate from "stale" on purpose. A database that
    has been migrated but never collected anything is not broken, and
    showing it in alarm red says something is wrong when nothing is -- the
    first thing anyone sees after a successful deploy should not look like
    a failure. It says what to do instead.
    """
    if not health:
        return "unknown", "no data in the database yet"

    age = health.get("quote_age_minutes")
    limit = float(os.getenv("MAX_QUOTE_AGE_MIN", "90"))

    if age is None and not last_run:
        return ("not started",
                "the tables are ready and nothing has been collected yet. "
                "Run the ETL workflow in GitHub Actions to begin")
    if age is None:
        return "no data", "runs have happened but no quote was stored"
    if float(age) > limit:
        return "stale", f"the newest quote is {int(float(age))} minutes old"
    if last_run and last_run.get("status") == "failed":
        return "failing", "the last ETL run failed"
    if last_run and last_run.get("status") == "partial":
        return "degraded", "the last ETL run was only partly successful"

    rate = health.get("success_rate_24h")
    if rate is not None and float(rate) < 80:
        return "degraded", f"API success rate is {float(rate):.0f}% over 24h"

    return "healthy", "data is current and the last run succeeded"


def collect() -> dict:
    """Everything the page and the API both need."""
    health = db.query_one(HEALTH_SQL)
    runs = db.query(LAST_RUNS_SQL)
    last_run = runs[0] if runs else None
    state, reason = _overall_state(health, last_run)

    return {
        "state": state,
        "reason": reason,
        "health": health or {},
        "runs": runs,
        "quotes": db.query(QUOTES_SQL),
        "sources": db.query(SOURCES_SQL),
        "errors": db.query(ERRORS_SQL),
        "coverage": db.query(COVERAGE_SQL),
        "max_age_min": float(os.getenv("MAX_QUOTE_AGE_MIN", "90")),
        # The page clock. Every other timestamp on the page comes out of
        # the database already converted (the *_local columns), so this one
        # has to be in the same zone or the masthead contradicts the tables.
        "generated_at": datetime.now(_display_zone()),
        "timezone_label": _timezone_label(),
        "timezone_name": db.app_timezone(),
    }


# ------------------------------------------------------------------ filters
def _register_filters(app: Flask) -> None:
    """Formatting belongs in one place, not scattered through the template."""

    @app.template_filter("dt")
    def _dt(value):
        if value is None:
            return "—"
        return value.strftime("%d %b %H:%M")

    @app.template_filter("day")
    def _day(value):
        return "—" if value is None else value.strftime("%d %b %Y")

    @app.template_filter("age")
    def _age(minutes):
        """Minutes as something readable, without false precision."""
        if minutes is None:
            return "—"
        m = float(minutes)
        if m < 1:
            return "just now"
        if m < 60:
            return f"{int(m)} min"
        if m < 1440:
            return f"{m / 60:.1f} h"
        return f"{m / 1440:.1f} d"

    @app.template_filter("money")
    def _money(value):
        return "—" if value is None else f"{float(value):,.2f}"

    @app.template_filter("pct")
    def _pct(value):
        if value is None:
            return "—"
        return f"{float(value):+.2f}%"

    @app.template_filter("budget_pct")
    def _budget_pct(age_minutes, limit, started=True):
        """
        How much of the staleness allowance is used, capped at 100.

        A pipeline that has never run gets 0, not 100: a full red bar reads
        as "critically overdue" when the truth is "nothing has happened
        yet".
        """
        if age_minutes is None:
            return 100.0 if started else 0.0
        if not limit:
            return 100.0
        return max(0.0, min(100.0, float(age_minutes) / float(limit) * 100.0))

    @app.template_filter("tone")
    def _tone(state):
        return {
            "healthy": "ok",
            "degraded": "warn",
            "stale": "warn",
            # Nothing is wrong yet, so nothing should be red.
            "not started": "idle",
            "unknown": "idle",
        }.get(state, "bad")

    @app.template_filter("run_tone")
    def _run_tone(status):
        return {"success": "ok", "partial": "warn", "running": "warn"}.get(status, "bad")


# ------------------------------------------------------------------ factory
def create_app() -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    _register_filters(app)

    @app.get("/health")
    def health():
        """
        Liveness for Render's health check. Spelled /health -- the previous
        version of this project pinged /healt and got a 404 288 times a day
        without anyone noticing.
        """
        try:
            db.query_one("SELECT 1 AS ok")
        except Exception as exc:
            logger.warning("health check could not reach the database: %s", exc)
            return jsonify({"status": "degraded",
                            "database": "unreachable",
                            "detail": str(exc)[:200]}), 503
        return jsonify({"status": "ok", "database": "reachable"})

    @app.get("/")
    def index():
        if not db.database_url():
            return render_template("error.html",
                                   title="No database configured",
                                   detail="DATABASE_URL is not set for this service. "
                                          "Add it in the Render dashboard under "
                                          "Environment, then redeploy."), 500
        try:
            if not db.schema_present():
                return render_template(
                    "error.html",
                    title="The tables are not here yet",
                    detail="This database has no bi_* views. Run the Database "
                           "workflow with action = migrate in GitHub Actions, "
                           "or run market_pipeline_setup.sql against it."), 503
            return render_template("index.html", **collect())
        except db.SchemaMissing as exc:
            return render_template("error.html",
                                   title="A table or view is missing",
                                   detail=str(exc)), 503
        except Exception as exc:
            logger.exception("dashboard failed")
            return render_template("error.html",
                                   title="Could not read the database",
                                   detail=str(exc)[:300]), 500

    @app.get("/api/status")
    def api_status():
        """The same data as JSON, for the monitor or anything else."""
        try:
            data = collect()
        except db.SchemaMissing as exc:
            return jsonify({"error": "schema missing", "detail": str(exc)}), 503
        except Exception as exc:
            logger.exception("api/status failed")
            return jsonify({"error": "database error", "detail": str(exc)[:300]}), 500

        return jsonify({
            "state": data["state"],
            "reason": data["reason"],
            "generated_at": data["generated_at"].isoformat(),
            # An ISO timestamp with an offset is unambiguous, but a consumer
            # still has to be told which clock the *_local columns below are
            # on, because those carry no offset at all.
            "timezone": data["timezone_name"],
            "timezone_label": data["timezone_label"],
            "health": {k: _jsonable(v) for k, v in data["health"].items()},
            "runs": _clean(data["runs"]),
            "quotes": _clean(data["quotes"]),
            "sources": _clean(data["sources"]),
            "errors": _clean(data["errors"]),
            "coverage": _clean(data["coverage"]),
        })

    @app.teardown_appcontext
    def _teardown(exception=None):
        # Connections go back to the pool inside read_cursor; nothing to do
        # here, but the hook documents that the pool outlives the request.
        return None

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
