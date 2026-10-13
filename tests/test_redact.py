"""
Credential scrubbing, and the export guard that backs it up.

This exists because of a leak that was already live, not a theoretical
one. `requests` puts the full request URL into the string form of a
connection error, Twelve Data and Alpha Vantage carry the API key in the
query string, and that string was written straight into
log_api_call.error_msg -- a column read by the dashboard, the Power BI
model, and now a data export published to a public branch.

A key that has been published cannot be unpublished; it can only be
rotated. So these tests check the thing that prevents it being written,
and the thing that refuses to publish it if it was.
"""

from __future__ import annotations

import gzip
import pathlib
import sys
import zipfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

REAL_KEY = "cq3nf9pr01qhb7k2m8x4"
REAL_PASSWORD = "xK9mQ2vLp7RtZwNc3Bf1"
REAL_DSN = (f"postgresql://marketdb_user:{REAL_PASSWORD}"
            f"@dpg-abc123.frankfurt-postgres.render.com/marketdb")


@pytest.fixture
def holding_secrets(monkeypatch):
    """A process that holds the real keys, as the ETL does."""
    monkeypatch.setenv("TWELVEDATA_API_KEY", REAL_KEY)
    monkeypatch.setenv("DATABASE_URL", REAL_DSN)
    from pipeline import redact
    return redact


# ----------------------------------------------------------- layer 1: values
def test_a_configured_key_is_removed_wherever_it_appears(holding_secrets):
    """
    The strongest layer: the literal value is masked however it got there.
    This is what covers a parameter name nobody anticipated.
    """
    for text in (
        f"ConnectionError: Max retries exceeded with url: "
        f"/time_series?symbol=AAPL&apikey={REAL_KEY}",
        f"weird_provider_field: {REAL_KEY}",
        f"the key is {REAL_KEY} and it should not be here",
        f"X-Custom-Auth-Header: {REAL_KEY}",
    ):
        out = holding_secrets.scrub(text)
        assert REAL_KEY not in out, f"key survived in: {out}"


def test_the_database_password_is_removed_on_its_own(holding_secrets):
    """
    psycopg2 can put the whole DSN in a message. Masking only the complete
    string would miss the case where just the password appears.
    """
    assert REAL_PASSWORD not in holding_secrets.scrub(
        f"OperationalError: password authentication failed; tried "
        f"{REAL_PASSWORD} against the server")
    assert REAL_PASSWORD not in holding_secrets.scrub(
        f"could not connect: {REAL_DSN}")


# ------------------------------------------------------------ layer 2: shape
def test_a_key_this_process_does_not_hold_is_still_removed(monkeypatch):
    """
    The second layer. A URL built elsewhere, or a key rotated since this
    process started, is caught by shape rather than by value.
    """
    for name in ("FINNHUB_API_KEY", "ALPHAVANTAGE_API_KEY",
                 "TWELVEDATA_API_KEY", "DATABASE_URL", "PGPASSWORD"):
        monkeypatch.delenv(name, raising=False)
    from pipeline import redact

    out = redact.scrub(
        "HTTPError: 401 for url: https://finnhub.io/api/v1/quote"
        "?symbol=AAPL&token=someoneelseskey12345")
    assert "someoneelseskey12345" not in out
    assert "token=***" in out

    out = redact.scrub(f"connection refused: {REAL_DSN}")
    assert REAL_PASSWORD not in out


def test_a_bearer_token_is_removed(monkeypatch):
    from pipeline import redact
    out = redact.scrub("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.abcdefghij")
    assert "eyJhbGciOiJSUzI1NiJ9.abcdefghij" not in out


# ------------------------------------------------------------- not too eager
def test_an_ordinary_error_is_left_alone(holding_secrets):
    """
    A scrubber that mangles normal messages is worse than none: the whole
    point of error_msg is that someone can read it and know what broke.
    """
    for text in (
        "rate limit exceeded",
        "twelvedata time_series: server returned 503",
        "Thank you for using Alpha Vantage! Our standard API rate limit is "
        "25 requests per day.",
        "ConnectionError: HTTPSConnectionPool(host='api.twelvedata.com', "
        "port=443): Read timed out.",
        "finnhub profile AAPL: 2 rows",
    ):
        assert holding_secrets.scrub(text) == text, f"needlessly altered: {text}"


def test_none_stays_none(holding_secrets):
    assert holding_secrets.scrub(None) is None


def test_short_values_are_not_treated_as_secrets(monkeypatch):
    """
    A one-character key in a test environment must not turn every word
    containing it into a mask.
    """
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    from pipeline import redact
    assert redact.scrub("extra context here") == "extra context here"


def test_the_limit_is_applied_after_scrubbing(holding_secrets):
    """
    Truncating first could cut a key in half and leave the first half
    visible. Scrubbing first means there is nothing left to cut.
    """
    text = f"prefix {REAL_KEY} " + "x" * 2000
    out = holding_secrets.scrub(text, limit=50)
    assert len(out) == 50
    assert REAL_KEY not in out


# --------------------------------------------------- it reaches the database
def test_a_leaky_error_is_scrubbed_on_its_way_into_the_log(pipeline, monkeypatch):
    """
    The end-to-end version: whatever a collector passes to db.log_api_call
    must not be in the table afterwards.
    """
    monkeypatch.setenv("TWELVEDATA_API_KEY", REAL_KEY)
    import importlib

    from pipeline import redact
    importlib.reload(redact)

    pipeline.db.log_api_call(
        source="twelvedata", symbol=None, endpoint="time_series",
        http_status=None, response_ms=12,
        error=f"ConnectionError: Max retries exceeded with url: "
              f"/time_series?symbol=AAPL&apikey={REAL_KEY}",
    )
    stored = pipeline.scalar(
        "SELECT error_msg FROM log_api_call ORDER BY log_id DESC LIMIT 1")
    assert REAL_KEY not in stored
    assert "apikey=***" in stored


def test_a_fatal_run_error_is_scrubbed_too(pipeline, monkeypatch):
    """close_run takes exception text, which can be a whole DSN."""
    monkeypatch.setenv("DATABASE_URL", REAL_DSN)
    import importlib

    from pipeline import redact
    importlib.reload(redact)

    run_id = pipeline.db.open_run("test-redact", "test", 1)
    pipeline.db.close_run(run_id, "failed", 1.0, 0, 1, 0,
                          f"OperationalError: {REAL_DSN} is unreachable")
    stored = pipeline.scalar(
        "SELECT error_msg FROM log_pipeline_run WHERE run_id = %s", (run_id,))
    assert REAL_PASSWORD not in stored


# ------------------------------------------------------- the publish backstop
def _export_folder(tmp_path: pathlib.Path, csv_body: str,
                   sheet_body: str = "clean") -> pathlib.Path:
    out = tmp_path / "export"
    (out / "csv").mkdir(parents=True)
    (out / "manifest.json").write_text('{"timezone": "Europe/Berlin"}')
    with gzip.open(out / "csv" / "log_api_call.csv.gz", "wt") as fh:
        fh.write("log_id,error_msg\n" + csv_body + "\n")
    with zipfile.ZipFile(out / "market_data.xlsx", "w") as zf:
        zf.writestr("xl/sharedStrings.xml",
                    f"<sst><si><t>{sheet_body}</t></si></sst>")
    return out


def _check(folder: pathlib.Path) -> int:
    from tools import check_export_safe
    return check_export_safe.main(["check_export_safe.py", str(folder)])


def test_the_guard_passes_a_clean_export(tmp_path):
    assert _check(_export_folder(tmp_path, "1,rate limit exceeded")) == 0


def test_the_guard_passes_an_already_scrubbed_export(tmp_path):
    """`***` is what redact.py leaves behind; it must not trip the guard."""
    folder = _export_folder(
        tmp_path, "1,\"Max retries with url: /q?apikey=***\"",
        "connection to postgresql://user:***@host/db failed")
    assert _check(folder) == 0


def test_the_guard_finds_a_key_inside_a_gzipped_csv(tmp_path):
    """
    The reason this is a tool and not a grep: `grep -I` skips the
    compressed file entirely, so the key would be published.
    """
    folder = _export_folder(tmp_path, f"1,\"url: /q?apikey={REAL_KEY}\"")
    assert _check(folder) == 1


def test_the_guard_finds_a_dsn_inside_a_gzipped_csv(tmp_path):
    folder = _export_folder(tmp_path, f"1,\"could not connect: {REAL_DSN}\"")
    assert _check(folder) == 1


def test_the_guard_finds_a_key_inside_the_workbook(tmp_path):
    """A workbook is a zip of XML; cell text lives in sharedStrings.xml."""
    folder = _export_folder(tmp_path, "1,fine",
                            sheet_body=f"url: /q?token={REAL_KEY}")
    assert _check(folder) == 1


def test_the_guard_fails_when_there_is_nothing_to_check(tmp_path):
    """
    Silence on a missing folder would let a broken export publish nothing
    and still report success.
    """
    assert _check(tmp_path / "not-here") == 1
