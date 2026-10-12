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


def test_date_dimension_covers_today_and_the_future(database):
    """Earnings dates are in the future; the calendar must reach them."""
    with database.cursor() as cur:
        cur.execute("SELECT MIN(full_date), MAX(full_date) FROM bi_dim_date")
        lo, hi = cur.fetchone()
        cur.execute("SELECT CURRENT_DATE")
        today = cur.fetchone()[0]
    assert lo <= today <= hi
    assert (hi - today).days >= 365, "calendar should extend at least a year out"


def test_date_keys_are_consistent(database):
    """date_key is the only join to the calendar; the format must match."""
    with database.cursor() as cur:
        cur.execute("SELECT date_key, full_date FROM bi_dim_date "
                    "WHERE full_date = CURRENT_DATE")
        key, full = cur.fetchone()
    assert key == int(full.strftime("%Y%m%d"))
