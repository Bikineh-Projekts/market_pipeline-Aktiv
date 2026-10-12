"""
Run orchestration and the run log.

The run log is the only thing the monitor and Power BI see, so its status
has to be honest: a half-working run must not report success, and a spent
Alpha Vantage quota must not report failure.
"""

from __future__ import annotations

import json
import os

from tests import mock_provider


def test_healthy_run_is_success(pipeline):
    code = pipeline.run.main(["--trigger", "test"])
    status, ok, failed, error = pipeline.last_run()

    assert code == 0
    assert status == "success"
    assert (ok, failed) == (2, 0)
    assert error == ""


def test_full_run_collects_every_fact_table(pipeline):
    pipeline.run.main(["--full", "--trigger", "test"])
    for table in ("fact_market_quote", "fact_market_timeseries",
                  "fact_market_indicator", "fact_company_fundamental",
                  "fact_earnings_calendar"):
        assert pipeline.scalar(f"SELECT COUNT(*) FROM {table}") > 0, f"{table} is empty"


def test_broken_core_source_reports_partial_not_success(pipeline):
    """
    Candles still arrive when Finnhub is down, so this is not a failure --
    but reporting it as a clean success would hide a broken provider.
    """
    mock_provider.FAULTS["finnhub_status"] = 500
    code = pipeline.run.main(["--trigger", "test"])
    status, ok, failed, error = pipeline.last_run()

    assert code == 0, "data did arrive, so the job should not go red"
    assert status == "partial", "a half-working run must not say success"
    assert "finnhub" in error, "the run log must name the broken provider"
    # Both symbols still got their candles, so they count as collected --
    # `partial` plus the recorded error is what flags the outage.
    assert ok == 2
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_market_quote") == 0


def test_total_outage_is_failure_with_exit_1(pipeline):
    """No data at all must turn the CI job red."""
    mock_provider.FAULTS["finnhub_status"] = 500
    mock_provider.FAULTS["twelvedata_error"] = True

    code = pipeline.run.main(["--trigger", "test"])
    status, ok, failed, _ = pipeline.last_run()

    assert code == 1
    assert status == "failed"
    assert ok == 0


def test_spent_indicator_quota_stays_a_success(pipeline):
    """Indicators are a bonus. Losing them must not fail the nightly run."""
    mock_provider.FAULTS["alphavantage_quota"] = True
    code = pipeline.run.main(["--symbols", "AAPL", "--indicators", "--trigger", "test"])
    status, ok, _, error = pipeline.last_run()

    assert code == 0
    assert status == "success"
    assert ok == 1
    assert "alphavantage" in error, "the quota loss should still be recorded"


def test_prices_survive_a_lost_quota(pipeline):
    mock_provider.FAULTS["alphavantage_quota"] = True
    pipeline.run.main(["--symbols", "AAPL", "--indicators", "--trigger", "test"])
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_market_quote") == 1
    assert pipeline.scalar("SELECT COUNT(*) FROM fact_market_timeseries") > 0


def test_each_run_adds_its_own_log_row(pipeline):
    """
    run_uid is UNIQUE with ON CONFLICT DO UPDATE, so a repeated uid
    overwrites history instead of appending. Rapid runs must stay distinct.
    """
    for i in range(4):
        pipeline.run.main(["--symbols", "AAPL", "--trigger", f"run-{i}"])

    assert pipeline.scalar("SELECT COUNT(*) FROM log_pipeline_run") == 4
    assert pipeline.scalar("SELECT COUNT(DISTINCT run_uid) FROM log_pipeline_run") == 4


def test_github_run_attempt_distinguishes_reruns(monkeypatch):
    """
    GITHUB_RUN_ID is unchanged when a workflow run is re-run. Without the
    attempt number, retrying a failed run erases the evidence of why it
    failed.
    """
    import importlib

    monkeypatch.setenv("GITHUB_RUN_ID", "18234567890")

    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    from pipeline import config
    first = importlib.reload(config).RUN_UID

    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    second = importlib.reload(config).RUN_UID

    assert first != second
    assert first.endswith("-1") and second.endswith("-2")


def test_run_is_closed_with_timing_and_counts(pipeline):
    pipeline.run.main(["--symbols", "AAPL", "--trigger", "test"])
    row = pipeline.query("""
        SELECT finished_at_utc IS NOT NULL, duration_seconds >= 0,
               rows_written > 0, status
          FROM log_pipeline_run ORDER BY run_id DESC LIMIT 1
    """)[0]
    assert row == (True, True, True, "success")


def test_api_calls_are_linked_to_their_run(pipeline):
    """Without run_id you cannot tell which run an error belongs to."""
    pipeline.run.main(["--symbols", "AAPL", "--trigger", "test"])
    unlinked = pipeline.scalar("SELECT COUNT(*) FROM log_api_call WHERE run_id IS NULL")
    assert unlinked == 0


def test_status_file_is_written(pipeline, tmp_path, monkeypatch):
    """The status file is the hand-off to the monitor and CI artifact."""
    target = tmp_path / "status.json"
    monkeypatch.setattr(pipeline.config, "STATUS_FILE", str(target))

    pipeline.run.main(["--symbols", "AAPL", "--trigger", "test"])

    payload = json.loads(target.read_text())
    assert payload["status"] == "success"
    assert payload["symbols_ok"] == 1
    assert payload["rows_written"] > 0
    assert payload["run_uid"]


def test_symbols_flag_overrides_the_environment(pipeline):
    pipeline.run.main(["--symbols", "TSLA", "--trigger", "test"])
    codes = [r[0] for r in pipeline.query("SELECT symbol_code FROM dim_symbol")]
    assert codes == ["TSLA"]


def test_missing_database_config_fails_cleanly(pipeline, monkeypatch):
    """A clear exit beats a stack trace in a scheduled run's log."""
    monkeypatch.setattr(pipeline.config, "DATABASE_URL", "")
    monkeypatch.setattr(pipeline.config, "PGHOST", "")
    monkeypatch.setattr(pipeline.config, "PGDATABASE", "")
    monkeypatch.setattr(pipeline.config, "PGUSER", "")

    assert pipeline.run.main(["--trigger", "test"]) == 1


def test_backfill_flag_requests_more_history(pipeline):
    pipeline.run.main(["--symbols", "AAPL", "--backfill", "120", "--trigger", "test"])
    daily = pipeline.scalar("""
        SELECT COUNT(*) FROM fact_market_timeseries t
          JOIN dim_interval i USING (interval_id)
         WHERE i.interval_code = '1day'
    """)
    assert daily == 120
