"""
Replication from one copy of the schema to another.

The failure this suite is built around is silent. symbol_id, source_id,
interval_id and indicator_id are SERIAL, so the same symbol can be id 2 in
one database and id 7 in the other -- and it WILL be, because 01_schema.sql
seeds three of those dimensions, so a freshly created target already holds
those rows under ids it assigned itself.

Copy a fact's raw symbol_id into such a database and the foreign key is
satisfied, no error is raised, and the price is attached to the wrong
company. Row counts match. The audit passes. Every number in the report is
wrong.

So the tests below deliberately renumber the target's dimensions before
replicating, and then check the data by symbol_code and source_name rather
than by id.
"""

from __future__ import annotations

import datetime
import pathlib
import sys

import psycopg2
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.conftest import TEST_URL  # noqa: E402
from tools import replicate  # noqa: E402


class _FakeResponse:
    """Just enough of urlopen's context manager for the keep-awake tests."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

TARGET_URL = TEST_URL.replace("/market_pipeline_test", "/market_pipeline_test_target")
TARGET_DB = "market_pipeline_test_target"


def _admin_url() -> str:
    head, _ = TEST_URL.rsplit("/", 1)
    return f"{head}/postgres"


@pytest.fixture
def target():
    """
    A second, empty copy of the schema -- standing in for the Postgres on
    the user's own machine.

    Rebuilt per test: these tests count rows, and a copy left behind by the
    previous one would make every count meaningless.
    """
    admin = psycopg2.connect(_admin_url(), connect_timeout=10)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            " WHERE datname = %s AND pid <> pg_backend_pid()", (TARGET_DB,))
        cur.execute(f'DROP DATABASE IF EXISTS "{TARGET_DB}"')
        cur.execute(f'CREATE DATABASE "{TARGET_DB}"')
    admin.close()

    conn = psycopg2.connect(TARGET_URL, connect_timeout=10)
    conn.autocommit = True
    with conn.cursor() as cur:
        for name in ("sql/01_schema.sql", "sql/02_bi_views.sql"):
            cur.execute((REPO / name).read_text())
    yield conn
    conn.close()


def _skew_ids(conn) -> None:
    """
    Force every dimension id in the target to differ from the source's.

    This is not a contrived setup. Inserting one symbol locally, or
    restoring from a dump, is enough to shift the sequences -- and then
    every id the source hands over means something else here.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE dim_source    SET source_id    = source_id    + 100")
        cur.execute("UPDATE dim_interval  SET interval_id  = interval_id  + 200")
        cur.execute("UPDATE dim_indicator SET indicator_id = indicator_id + 300")
        cur.execute("SELECT setval('dim_source_source_id_seq', 200)")
        cur.execute("SELECT setval('dim_interval_interval_id_seq', 300)")
        cur.execute("SELECT setval('dim_indicator_indicator_id_seq', 400)")
        cur.execute(
            "INSERT INTO dim_symbol (symbol_code, company_name) "
            "VALUES ('ZZZ1','placeholder'),('ZZZ2','placeholder'),"
            "       ('ZZZ3','placeholder'),('ZZZ4','placeholder')")


def _replicate(**kwargs) -> int:
    from tools import replicate

    argv = ["--source", TEST_URL, "--target", TARGET_URL, "--once"]
    for key, value in kwargs.items():
        flag = "--" + key.replace("_", "-")
        argv.append(flag) if value is True else argv.extend([flag, str(value)])
    return replicate.main(argv)


def _rows(conn, sql: str, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _scalar(conn, sql: str, params=None):
    return _rows(conn, sql, params)[0][0]


# ------------------------------------------------------------------- basics
def test_replication_copies_every_table(pipeline, target):
    pipeline.run.main(["--full", "--trigger", "test"])
    assert _replicate() == 0

    for table in ("dim_symbol", "fact_market_timeseries", "fact_market_quote",
                  "fact_market_indicator", "fact_company_fundamental",
                  "fact_earnings_calendar", "log_pipeline_run", "log_api_call"):
        here = pipeline.scalar(f"SELECT COUNT(*) FROM {table}")
        there = _scalar(target, f"SELECT COUNT(*) FROM {table}")
        assert there == here, f"{table}: source has {here}, copy has {there}"


def test_prices_stay_attached_to_the_right_company(pipeline, target):
    """
    The whole reason this script remaps ids instead of copying them.

    Checked by symbol_code, not symbol_id: comparing ids would pass even
    when every price had been moved to the wrong company.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _skew_ids(target)
    assert _replicate() == 0

    source_closes = dict(pipeline.query("""
        SELECT s.symbol_code, ROUND(SUM(t.close)::numeric, 4)
          FROM fact_market_timeseries t JOIN dim_symbol s USING (symbol_id)
         GROUP BY s.symbol_code
    """))
    target_closes = dict(_rows(target, """
        SELECT s.symbol_code, ROUND(SUM(t.close)::numeric, 4)
          FROM fact_market_timeseries t JOIN dim_symbol s USING (symbol_id)
         GROUP BY s.symbol_code
    """))
    assert target_closes == source_closes, (
        "prices moved to the wrong company: the dimension ids were copied "
        "rather than translated"
    )


def test_every_fact_points_at_a_real_dimension_row(pipeline, target):
    pipeline.run.main(["--full", "--trigger", "test"])
    _skew_ids(target)
    _replicate()

    orphans = _scalar(target, """
        SELECT (SELECT COUNT(*) FROM fact_market_timeseries t
                 WHERE NOT EXISTS (SELECT 1 FROM dim_symbol s
                                    WHERE s.symbol_id = t.symbol_id))
             + (SELECT COUNT(*) FROM fact_market_indicator f
                 WHERE NOT EXISTS (SELECT 1 FROM dim_indicator i
                                    WHERE i.indicator_id = f.indicator_id))
             + (SELECT COUNT(*) FROM fact_market_quote q
                 WHERE NOT EXISTS (SELECT 1 FROM dim_source d
                                    WHERE d.source_id = q.source_id))
    """)
    assert orphans == 0


def test_intervals_and_indicators_are_translated_too(pipeline, target):
    """
    A candle copied under the wrong interval_id lands in the intraday view
    instead of the daily one, or vanishes from both.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _skew_ids(target)
    _replicate()

    source_mix = sorted(pipeline.query("""
        SELECT i.interval_code, COUNT(*) FROM fact_market_timeseries t
          JOIN dim_interval i USING (interval_id) GROUP BY 1
    """))
    target_mix = sorted(_rows(target, """
        SELECT i.interval_code, COUNT(*) FROM fact_market_timeseries t
          JOIN dim_interval i USING (interval_id) GROUP BY 1
    """))
    assert target_mix == source_mix

    source_ind = sorted(pipeline.query("""
        SELECT i.indicator_name, COUNT(*) FROM fact_market_indicator f
          JOIN dim_indicator i USING (indicator_id) GROUP BY 1
    """))
    target_ind = sorted(_rows(target, """
        SELECT i.indicator_name, COUNT(*) FROM fact_market_indicator f
          JOIN dim_indicator i USING (indicator_id) GROUP BY 1
    """))
    assert target_ind == source_ind


# -------------------------------------------------------------- idempotence
def test_running_it_again_changes_nothing(pipeline, target):
    """
    The one that would ruin the Power BI model: a second pass duplicating
    the grain doubles every measure, and the row counts still "look fine"
    to anyone who does not compare them.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _skew_ids(target)
    _replicate()

    before = {t: _scalar(target, f"SELECT COUNT(*) FROM {t}") for t in (
        "fact_market_timeseries", "fact_market_quote", "fact_market_indicator",
        "fact_company_fundamental", "fact_earnings_calendar",
        "log_pipeline_run", "log_api_call", "dim_symbol")}

    for _ in range(3):
        _replicate()

    after = {t: _scalar(target, f"SELECT COUNT(*) FROM {t}") for t in before}
    assert after == before


def test_a_full_pass_is_also_idempotent(pipeline, target):
    """
    --full re-reads every row; it must still not duplicate any.

    This used to check fact_market_timeseries alone, and passed while
    --full was doubling log_api_call every time it ran -- 39 rows to 78 to
    117. fact_market_timeseries has a unique constraint, so ON CONFLICT
    absorbed the second read and the property looked proven; log_api_call
    has none, which is the only case where the question was interesting.
    So: every table, named by the code rather than by hand, so a table
    added later is covered without anyone remembering to add it here.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _replicate()

    tables = [f.table for f in replicate.FACTS] + [d.table for d in replicate.DIMENSIONS]
    before = {t: _scalar(target, f"SELECT COUNT(*) FROM {t}") for t in tables}
    assert before["log_api_call"] > 0, "nothing was logged; the test proves nothing"

    _replicate(full=True)
    _replicate(full=True)

    after = {t: _scalar(target, f"SELECT COUNT(*) FROM {t}") for t in tables}
    grew = {t: (before[t], after[t]) for t in tables if after[t] != before[t]}
    assert not grew, f"--full duplicated rows: {grew}"


def test_full_is_refused_rather_than_guessed_when_the_watermark_is_lost(
        pipeline, target):
    """
    Rows present, watermark gone: the one case --full cannot recover.

    There is no way to tell which source rows a table without a unique key
    already holds, so the choice is between guessing and saying so. It says
    so, and names the way out -- a silent doubling of the api log would
    surface much later as a wrong success rate in Power BI, not as an error.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _replicate()
    before = _scalar(target, "SELECT COUNT(*) FROM log_api_call")
    assert before > 0

    with target.cursor() as cur:
        cur.execute("DELETE FROM replication_state WHERE table_name = 'log_api_call'")

    _replicate(full=True)
    assert _scalar(target, "SELECT COUNT(*) FROM log_api_call") == before

    # And the recovery the warning names actually works.
    with target.cursor() as cur:
        cur.execute("TRUNCATE log_api_call")
    _replicate(full=True)
    assert _scalar(target, "SELECT COUNT(*) FROM log_api_call") == before


def test_the_grain_survives_replication(pipeline, target):
    """One row per (symbol, day) on the far side too, or measures double."""
    pipeline.run.main(["--full", "--trigger", "test"])
    _replicate()
    _replicate()

    rows = _scalar(target, "SELECT COUNT(*) FROM bi_fact_price_daily")
    distinct = _scalar(target, "SELECT COUNT(*) FROM (SELECT DISTINCT "
                               "symbol_id, date_key FROM bi_fact_price_daily) x")
    assert rows == distinct


# ------------------------------------------------------------- incrementally
def test_only_new_rows_are_copied_on_the_second_pass(pipeline, target):
    """
    What makes a short interval affordable. If a pass re-read everything,
    running it every ten seconds would mean re-reading the whole history
    every ten seconds.
    """
    from tools import replicate

    pipeline.run.main(["--full", "--trigger", "test"])
    _replicate()

    totals = replicate.one_pass(TEST_URL, TARGET_URL, batch=5000,
                                dry_run=False, full=False, source_label="test")
    read = sum(r for r, _w in totals.values())
    assert read <= 60, f"second pass re-read {read} rows; the watermark is not working"


def test_rows_added_later_arrive(pipeline, target):
    """
    A new row written after the first pass must reach the copy.

    The row is inserted directly rather than by running the pipeline twice:
    fact_market_quote is unique on (symbol_id, source_id, quote_time_utc)
    and the mock provider stamps quotes to the second, so two runs inside
    the same second upsert into one row and the test would pass or fail
    depending on how fast the machine is.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _replicate()
    before = _scalar(target, "SELECT COUNT(*) FROM fact_market_quote")

    with pipeline.db.cursor() as cur:
        cur.execute("""
            INSERT INTO fact_market_quote
                   (symbol_id, source_id, fetched_at_utc, quote_time_utc, price)
            SELECT s.symbol_id, d.source_id, NOW(),
                   TIMESTAMPTZ '2030-01-01 12:00:00+00', 123.45
              FROM dim_symbol s, dim_source d
             WHERE s.symbol_code = 'AAPL' AND d.source_name = 'finnhub'
        """)

    _replicate()
    assert _scalar(target, "SELECT COUNT(*) FROM fact_market_quote") == before + 1
    # float() because a NUMERIC column arrives as Decimal, and
    # Decimal('123.45') == 123.45 is False.
    assert float(_scalar(target, """
        SELECT price FROM fact_market_quote q JOIN dim_symbol s USING (symbol_id)
         WHERE q.quote_time_utc = TIMESTAMPTZ '2030-01-01 12:00:00+00'
           AND s.symbol_code = 'AAPL'
    """)) == 123.45


def test_a_run_that_finishes_later_is_updated_not_duplicated(pipeline, target):
    """
    log_pipeline_run is INSERTed as 'running' and UPDATEd when the run
    ends. Without the overlap window the copy would hold every run at
    'running' forever; without conflicting on run_uid it would hold two
    rows per run.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _replicate()

    with pipeline.db.cursor() as cur:
        cur.execute("UPDATE log_pipeline_run SET status = 'partial', "
                    "error_msg = 'changed after the copy'")
    _replicate()

    rows = _rows(target, "SELECT run_uid, status FROM log_pipeline_run")
    assert len(rows) == len(set(r[0] for r in rows)), "duplicated a run"
    assert {r[1] for r in rows} == {"partial"}, "the update never arrived"


# ------------------------------------------------------------------- safety
def test_dry_run_writes_nothing(pipeline, target):
    pipeline.run.main(["--full", "--trigger", "test"])
    assert _replicate(dry_run=True) == 0
    assert _scalar(target, "SELECT COUNT(*) FROM fact_market_timeseries") == 0
    assert _scalar(target, "SELECT COUNT(*) FROM dim_symbol") == 0


def test_the_source_is_never_written_to(pipeline, target):
    """The source opens read-only, so a bug cannot damage production."""
    from tools import replicate

    conn = replicate.connect(TEST_URL, "source", read_only=True)
    try:
        with conn.cursor() as cur, pytest.raises(psycopg2.InternalError):
            cur.execute("INSERT INTO dim_symbol (symbol_code) VALUES ('NOPE')")
    finally:
        conn.close()


def test_it_refuses_a_target_without_the_schema(pipeline):
    """A clear message beats a confusing failure on the first INSERT."""
    from tools import replicate

    admin = psycopg2.connect(_admin_url(), connect_timeout=10)
    admin.autocommit = True
    bare = "market_pipeline_test_bare"
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{bare}"')
        cur.execute(f'CREATE DATABASE "{bare}"')
    admin.close()

    bare_url = TEST_URL.replace("/market_pipeline_test", f"/{bare}")
    with pytest.raises(SystemExit) as exc:
        replicate.main(["--source", TEST_URL, "--target", bare_url, "--once"])
    assert "missing" in str(exc.value)
    assert "01_schema.sql" in str(exc.value), "the message should say how to fix it"


def test_it_refuses_to_replicate_a_database_onto_itself():
    from tools import replicate

    with pytest.raises(SystemExit):
        replicate.main(["--source", TEST_URL, "--target", TEST_URL, "--once"])


def test_sequences_are_left_above_the_data(pipeline, target):
    """
    Otherwise the first insert the user makes locally fails on a duplicate
    key, which looks like a corrupt database and is only a stale sequence.
    """
    pipeline.run.main(["--full", "--trigger", "test"])
    _replicate()

    behind = _scalar(target, """
        SELECT COUNT(*) FROM (
          SELECT last_value, (SELECT MAX(symbol_id) FROM dim_symbol) AS hi
            FROM dim_symbol_symbol_id_seq) x
         WHERE last_value < hi
    """)
    assert behind == 0


# ---------------------------------------------------------------------
# KEEP-AWAKE
#
# A free Render web service spins down after 15 minutes idle. These check
# the throttle, because getting it wrong is expensive rather than merely
# broken: the free plan allows 750 instance-hours a month and a service
# kept awake around the clock spends about 744 of them.
# ---------------------------------------------------------------------
def test_the_ping_is_throttled_to_once_every_ten_minutes(monkeypatch):
    """--loop 1 must not mean 86,400 requests a day."""
    calls = []
    monkeypatch.setattr(replicate.urllib.request, "urlopen",
                        lambda url, timeout=0: calls.append(url) or _FakeResponse())
    monkeypatch.setattr(replicate, "PING_HOURS_UTC", range(0, 24))

    keeper = replicate.KeepAwake("https://example.invalid")
    start = 1_000_000.0
    for second in range(0, 1800):            # half an hour of one-second passes
        keeper.maybe_ping(now=start + second)

    assert len(calls) == 3, f"expected a ping every 10 min, got {len(calls)}"
    assert calls[0].endswith("/health")


def test_no_pings_outside_the_daytime_window(monkeypatch):
    """Overnight hours would spend the monthly allowance for nothing."""
    calls = []
    monkeypatch.setattr(replicate.urllib.request, "urlopen",
                        lambda url, timeout=0: calls.append(url) or _FakeResponse())
    monkeypatch.setattr(replicate, "PING_HOURS_UTC", range(6, 21))

    class _Night(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, 3, 0, tzinfo=tz)

    monkeypatch.setattr(replicate, "datetime", _Night)
    assert replicate.KeepAwake("https://example.invalid").maybe_ping() is False
    assert calls == []


def test_an_unreachable_dashboard_does_not_stop_replication(monkeypatch):
    """
    The ping is a courtesy, not a dependency.

    Waking a spun-down instance regularly takes longer than the timeout --
    and the request that timed out is the one that woke it. Raising here
    would end the replication loop over a status page.
    """
    def explode(url, timeout=0):
        raise OSError("connection reset by peer")

    monkeypatch.setattr(replicate.urllib.request, "urlopen", explode)
    monkeypatch.setattr(replicate, "PING_HOURS_UTC", range(0, 24))

    assert replicate.KeepAwake("https://example.invalid").maybe_ping() is True
