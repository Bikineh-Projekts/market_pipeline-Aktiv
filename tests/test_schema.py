"""The SQL files must be safe to run on every deploy, forever."""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "dim_source", "dim_symbol", "dim_interval", "dim_indicator",
    "fact_market_quote", "fact_market_timeseries", "fact_market_indicator",
    "fact_company_fundamental", "fact_earnings_calendar",
    "log_api_call", "log_pipeline_run",
}


def _tables(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'")
        return {r[0] for r in cur.fetchall()}


def _views(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.views "
                    "WHERE table_schema='public' AND table_name LIKE 'bi\\_%'")
        return {r[0] for r in cur.fetchall()}


def test_every_expected_table_exists(database):
    assert EXPECTED_TABLES <= _tables(database)


def test_schema_is_idempotent(database):
    """The Database workflow re-runs migrate freely; it must never break."""
    before = _tables(database)
    with database.cursor() as cur:
        for _ in range(2):
            cur.execute((REPO / "sql/01_schema.sql").read_text())
            cur.execute((REPO / "sql/02_bi_views.sql").read_text())
    assert _tables(database) == before


def test_reapplying_schema_preserves_data(database):
    """A migrate must never be destructive."""
    with database.cursor() as cur:
        cur.execute("INSERT INTO dim_symbol (symbol_code, company_name) "
                    "VALUES ('KEEPME', 'Survivor') ON CONFLICT DO NOTHING")
        cur.execute((REPO / "sql/01_schema.sql").read_text())
        cur.execute("SELECT company_name FROM dim_symbol WHERE symbol_code='KEEPME'")
        assert cur.fetchone()[0] == "Survivor"
        cur.execute("DELETE FROM dim_symbol WHERE symbol_code='KEEPME'")


def test_bi_views_all_present(database):
    views = _views(database)
    # The Power BI model depends on each of these by name.
    required = {
        "bi_dim_date", "bi_dim_symbol", "bi_dim_source", "bi_dim_interval",
        "bi_dim_indicator", "bi_fact_price_daily", "bi_fact_price_intraday",
        "bi_fact_indicator_daily", "bi_fact_quote_latest",
        "bi_fact_quote_history", "bi_fact_fundamental_latest",
        "bi_fact_earnings", "bi_fact_api_call", "bi_fact_pipeline_run",
        "bi_pipeline_health", "bi_storage_usage",
    }
    assert required <= views, f"missing: {sorted(required - views)}"


def test_bi_views_are_all_queryable(database):
    """A view that compiles but errors on SELECT is useless to Power BI."""
    for view in sorted(_views(database)):
        with database.cursor() as cur:
            cur.execute(f"SELECT * FROM {view} LIMIT 1")


def _top_level_order_by(definition: str) -> bool:
    """
    True if the view sorts its own final result.

    Counting the string would be wrong: window functions
    (OVER (PARTITION BY ... ORDER BY ...)) and subqueries contain perfectly
    legitimate ORDER BY clauses. Only one at parenthesis depth zero sorts
    the view's output, and that is the kind Power BI pays for and discards.
    """
    upper = definition.upper()
    depth = 0
    i = 0
    while i < len(upper):
        char = upper[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and upper.startswith("ORDER BY", i):
            return True
        i += 1
    return False


def test_no_view_sorts_its_own_output(database):
    """
    ORDER BY at the top level of a view breaks Power BI's query folding and
    buys nothing -- Power BI discards the ordering and sorts in the visual.

    Checked against the definitions Postgres actually stored, not the source
    text, so a sort added by hand later is caught too. DISTINCT ON requires
    ORDER BY, so those views are exempt.
    """
    offenders = []
    with database.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.views "
                    "WHERE table_schema='public' AND table_name LIKE 'bi\\_%' "
                    "ORDER BY table_name")
        views = [r[0] for r in cur.fetchall()]

        for view in views:
            cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (view,))
            definition = cur.fetchone()[0]
            if "DISTINCT ON" in definition.upper():
                continue
            if _top_level_order_by(definition):
                offenders.append(view)

    assert offenders == [], f"these views sort their own output: {offenders}"


def test_no_jsonb_columns_exposed_to_power_bi(database):
    """Power BI imports jsonb as an unusable Record type."""
    with database.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema='public'
               AND table_name LIKE 'bi\\_%'
               AND data_type IN ('json', 'jsonb')
        """)
        assert cur.fetchall() == []


def test_date_dimension_covers_today(database):
    """Today must be in the calendar, or no current visual can filter."""
    with database.cursor() as cur:
        cur.execute("SELECT MIN(full_date), MAX(full_date) FROM bi_dim_date")
        lo, hi = cur.fetchone()
        cur.execute("SELECT local_today()")
        today = cur.fetchone()[0]
    assert lo <= today <= hi


def test_date_dimension_is_whole_calendar_years(database):
    """
    Power BI's time intelligence (YTD, SAMEPERIODLASTYEAR) returns wrong
    numbers on a date table whose first or last year is partial, so the
    bounds are snapped out to 1 January and 31 December.
    """
    with database.cursor() as cur:
        cur.execute("SELECT MIN(full_date), MAX(full_date) FROM bi_dim_date")
        lo, hi = cur.fetchone()
    assert (lo.month, lo.day) == (1, 1), f"calendar starts mid-year at {lo}"
    assert (hi.month, hi.day) == (12, 31), f"calendar ends mid-year at {hi}"


def test_date_dimension_has_no_years_without_data(database):
    """
    The regression this replaces: the calendar used to be a hardcoded
    2015-01-01 .. today + 2 years, independent of what had been collected.
    Measured on the live database, 9 of the 14 years it produced held no
    price at all -- and because a date dimension is generated rather than
    collected, every one of those years appeared in every Power BI date
    slicer with nothing behind it. That is what "I see irrelevant dates"
    was.

    With an empty database the only year allowed is the current one.
    """
    with database.cursor() as cur:
        cur.execute("SELECT DISTINCT year FROM bi_dim_date ORDER BY year")
        years = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT EXTRACT(YEAR FROM local_today())::int")
        this_year = cur.fetchone()[0]
    assert years == [this_year], (
        f"calendar spans {years} but nothing has been collected; only "
        f"{this_year} should be present"
    )


def _one_candle(cur, when: str) -> None:
    """A single daily candle at a chosen instant, on a throwaway symbol."""
    cur.execute("INSERT INTO dim_symbol (symbol_code, company_name) "
                "VALUES ('ZZTEST', 'Z Test Ltd') RETURNING symbol_id")
    symbol_id = cur.fetchone()[0]
    cur.execute("SELECT source_id FROM dim_source LIMIT 1")
    source_id = cur.fetchone()[0]
    cur.execute("SELECT interval_id FROM dim_interval "
                "WHERE interval_code IN ('1day', 'daily') LIMIT 1")
    interval_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO fact_market_timeseries
               (symbol_id, source_id, interval_id, candle_time_utc,
                open, high, low, close, volume)
        VALUES (%s, %s, %s, %s::timestamptz, 1, 2, 0.5, 1.5, 100)
    """, (symbol_id, source_id, interval_id, when))


def test_date_dimension_stretches_to_reach_old_data(database):
    """
    The flip side of the fix: it must still cover the history that IS
    there. A candle from 2022 has to be reachable, or the price it carries
    vanishes from every visual that filters by date -- which would be a
    worse bug than the one being fixed.
    """
    with database.cursor() as cur:
        _one_candle(cur, "2022-10-17 00:00:00+00")

        cur.execute("SELECT MIN(full_date), MAX(full_date) FROM bi_dim_date")
        lo, _hi = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*) FROM bi_fact_price_daily f
             WHERE NOT EXISTS (SELECT 1 FROM bi_dim_date d
                                WHERE d.date_key = f.date_key)
        """)
        orphans = cur.fetchone()[0]

    assert (lo.year, lo.month, lo.day) == (2022, 1, 1)
    assert orphans == 0, "a price row the calendar cannot match"


def test_a_late_evening_row_is_counted_on_the_local_day(database):
    """
    23:30 UTC is 01:30 the NEXT day in Berlin, so that is the day the row
    belongs to. Deriving date_key from the UTC day instead files every row
    collected after 22:00 (23:00 in winter) under the previous day -- which
    is wrong for roughly two hours out of every twenty-four and shows up
    as yesterday's bar moving after midnight.
    """
    with database.cursor() as cur:
        _one_candle(cur, "2026-01-15 23:30:00+00")
        cur.execute("SELECT date_key, trade_date FROM bi_fact_price_daily")
        date_key, trade_date = cur.fetchone()

    assert date_key == 20260116, f"filed under {date_key}, not the Berlin day"
    assert trade_date.isoformat() == "2026-01-16"


def test_date_dimension_flags_days_without_prices(database):
    """
    has_price_data lets a visual drop the days that exist only to complete
    a calendar year, without narrowing the table and breaking time
    intelligence.
    """
    with database.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM bi_dim_date WHERE has_price_data")
        assert cur.fetchone()[0] == 0  # nothing collected in this fixture


def test_date_keys_are_consistent(database):
    """date_key is the only join to the calendar; the format must match."""
    with database.cursor() as cur:
        cur.execute("SELECT date_key, full_date FROM bi_dim_date "
                    "WHERE full_date = CURRENT_DATE")
        key, full = cur.fetchone()
    assert key == int(full.strftime("%Y%m%d"))
