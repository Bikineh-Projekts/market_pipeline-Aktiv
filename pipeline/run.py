"""
Pipeline entrypoint. Runs exactly one collection cycle and exits.

    python -m pipeline.run                       quotes + daily candles
    python -m pipeline.run --indicators          also Alpha Vantage (uses quota)
    python -m pipeline.run --full                everything, for the nightly job
    python -m pipeline.run --backfill 2000       deep history load
    python -m pipeline.run --symbols AAPL,TSLA   override the symbol list

Exit code 0 when at least one symbol succeeded, 1 otherwise, so a CI job
turns red exactly when the data did not arrive.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import config, db
from .collectors import alphavantage, finnhub, twelvedata
from .collectors.base import ApiError, pause

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("pipeline")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def collect_symbol(symbol: str, options: argparse.Namespace, run_id: int | None) -> dict:
    """Collect everything requested for one symbol. Never raises."""
    started = time.perf_counter()
    result = {"symbol": symbol, "rows": 0, "errors": [], "core_errors": [], "steps": {}}

    # Price data is the point of the pipeline. Indicators are a bonus whose
    # quota errors should be visible but must not mark the run failed.
    core_steps = {"finnhub", "twelvedata"}

    steps = [
        ("finnhub", lambda: finnhub.run(
            symbol,
            with_fundamentals=options.fundamentals,
            with_earnings=options.earnings,
            run_id=run_id,
        )),
        ("twelvedata", lambda: twelvedata.run(
            symbol, daily_size=options.backfill or None, run_id=run_id
        )),
    ]

    if options.indicators:
        steps.append(("alphavantage", lambda: alphavantage.run(symbol, run_id=run_id)))

    for name, action in steps:
        try:
            rows = action()
            result["rows"] += rows
            result["steps"][name] = {"ok": True, "rows": rows}
        except ApiError as exc:
            message = f"{name}: {exc}"
            result["errors"].append(message)
            if name in core_steps:
                result["core_errors"].append(message)
            result["steps"][name] = {"ok": False, "error": str(exc)}
            print(f"    {name} failed: {exc}")
        except Exception as exc:
            message = f"{name}: {type(exc).__name__}: {exc}"
            result["errors"].append(message)
            if name in core_steps:
                result["core_errors"].append(message)
            result["steps"][name] = {"ok": False, "error": str(exc)}
            logger.exception("%s crashed for %s", name, symbol)

        pause(config.SLEEP_FINNHUB)

    # "ok" means usable data landed for this symbol. "degraded" means some
    # core source was broken even though other data arrived — that keeps a
    # half-working run from reporting itself as a clean success.
    result["ok"] = any(
        step.get("ok") for name, step in result["steps"].items() if name in core_steps
    )
    result["degraded"] = bool(result["core_errors"])
    result["duration_seconds"] = round(time.perf_counter() - started, 2)
    return result


def write_status(payload: dict) -> None:
    try:
        path = Path(config.STATUS_FILE)
        path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False),
                        encoding="utf-8")
        print(f"status written to {path.resolve()}")
    except Exception as exc:
        logger.warning("could not write status file: %s", exc)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market data pipeline — one cycle")
    parser.add_argument("--symbols", help="Comma separated, overrides SYMBOLS env")
    parser.add_argument("--indicators", action="store_true",
                        help="Fetch Alpha Vantage indicators (25 requests/day limit)")
    parser.add_argument("--fundamentals", action="store_true", help="Fetch company profile and metrics")
    parser.add_argument("--earnings", action="store_true", help="Fetch the earnings calendar")
    parser.add_argument("--full", action="store_true", help="Shorthand for all three of the above")
    parser.add_argument("--backfill", type=int, metavar="N",
                        help="Load N daily candles per symbol instead of the default")
    parser.add_argument("--trigger", default=config.RUN_TRIGGER)

    options = parser.parse_args(argv)

    if options.full:
        options.indicators = options.fundamentals = options.earnings = True

    return options


def main(argv=None) -> int:
    options = parse_args(argv)

    if not config.has_database_config():
        print("ERROR: no database configuration. Set DATABASE_URL.", file=sys.stderr)
        return 1

    missing = config.missing_keys()
    if missing:
        print(f"WARNING: missing API keys: {', '.join(missing)}", file=sys.stderr)

    symbols = ([s.strip().upper() for s in options.symbols.split(",") if s.strip()]
               if options.symbols else config.SYMBOLS)

    run_uid = config.RUN_UID or f"local-{int(time.time())}"
    started = _now()
    started_perf = time.perf_counter()

    print(f"\n{'=' * 62}")
    print(f"  run {run_uid}  ·  trigger {options.trigger}")
    print(f"  symbols: {', '.join(symbols)}")
    print(f"  indicators={options.indicators} fundamentals={options.fundamentals} "
          f"earnings={options.earnings} backfill={options.backfill or 'no'}")
    print(f"{'=' * 62}\n")

    run_id = None
    results: list[dict] = []
    fatal: str | None = None

    try:
        run_id = db.open_run(run_uid, options.trigger, len(symbols))

        for index, symbol in enumerate(symbols, start=1):
            print(f"  [{index}/{len(symbols)}] {symbol}")
            results.append(collect_symbol(symbol, options, run_id))
            if index < len(symbols):
                pause(config.SLEEP_TWELVEDATA, "between symbols")

    except KeyboardInterrupt:
        fatal = "interrupted by user"
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    ok = sum(1 for r in results if r["ok"])
    failed = len(results) - ok
    rows = sum(r["rows"] for r in results)
    duration = round(time.perf_counter() - started_perf, 2)

    degraded = sum(1 for r in results if r.get("degraded"))
    all_errors = "; ".join(f"{r['symbol']} {e}" for r in results for e in r["errors"])[:2000]

    if fatal:
        status = "failed"
        error = fatal
    elif ok == 0:
        status = "failed"
        error = all_errors or "no symbols collected"
    elif failed > 0 or degraded > 0:
        status = "partial"
        error = all_errors
    else:
        # Every symbol got its price data. Any remaining errors (typically
        # the Alpha Vantage daily quota) are recorded but are not a failure.
        status = "success"
        error = all_errors or None

    try:
        db.close_run(run_id, status, duration, ok, failed, rows, error)
    finally:
        db.close_connection()

    write_status({
        "run_uid": run_uid,
        "trigger": options.trigger,
        "status": status,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": _now().isoformat(),
        "duration_seconds": duration,
        "symbols_total": len(symbols),
        "symbols_ok": ok,
        "symbols_failed": failed,
        "rows_written": rows,
        "error": error,
        "results": results,
    })

    print(f"\n{'=' * 62}")
    print(f"  {status.upper()}  ·  {ok}/{len(symbols)} symbols  ·  {rows} rows  ·  {duration}s")
    print(f"{'=' * 62}\n")

    return 0 if status in ("success", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
