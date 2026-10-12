"""
Dimension upserts.

The regression test at the bottom of this file guards the worst bug this
project has had: the dimension cache served a hit to a call that carried a
company profile, so the profile was never written and every Power BI
slicer read "UNKNOWN" / "Uncategorized" forever.
"""

from __future__ import annotations


def test_symbol_is_created_once_and_reused(pipeline):
    first = pipeline.db.symbol_id("AAPL")
    second = pipeline.db.symbol_id("AAPL")
    assert first == second
    assert pipeline.scalar("SELECT COUNT(*) FROM dim_symbol WHERE symbol_code='AAPL'") == 1


def test_symbol_code_is_normalised_to_upper_case(pipeline):
    upper = pipeline.db.symbol_id("AAPL")
    lower = pipeline.db.symbol_id("aapl")
    assert upper == lower, "case differences must not create a duplicate symbol"
    assert pipeline.scalar("SELECT COUNT(*) FROM dim_symbol") == 1


def test_profile_is_written_after_a_bare_lookup(pipeline):
    """
    The real call order inside finnhub.run(): collect_quote looks the symbol
    up with no profile, then collect_fundamentals supplies the real one.
    The second call must reach the database.
    """
    pipeline.db.symbol_id("AAPL")  # bare lookup, as collect_quote does

    pipeline.db.symbol_id(
        "AAPL",
        company_name="Apple Inc",
        exchange="NASDAQ",
        country="US",
        currency="USD",
        sector="Technology",
    )

    row = pipeline.query(
        "SELECT company_name, exchange, country, currency, sector "
        "FROM dim_symbol WHERE symbol_code='AAPL'"
    )[0]
    assert row == ("Apple Inc", "NASDAQ", "US", "USD", "Technology")


def test_profile_survives_a_later_bare_lookup(pipeline):
    """A subsequent attribute-free lookup must not blank the profile."""
    pipeline.db.symbol_id("AAPL", company_name="Apple Inc", sector="Technology")
    pipeline.db.symbol_id("AAPL")

    row = pipeline.query(
        "SELECT company_name, sector FROM dim_symbol WHERE symbol_code='AAPL'"
    )[0]
    assert row == ("Apple Inc", "Technology")


def test_partial_profile_does_not_erase_known_fields(pipeline):
    """COALESCE in the upsert keeps fields the newer call omits."""
    pipeline.db.symbol_id("AAPL", company_name="Apple Inc", country="US")
    pipeline.db.symbol_id("AAPL", sector="Technology")

    row = pipeline.query(
        "SELECT company_name, country, sector FROM dim_symbol WHERE symbol_code='AAPL'"
    )[0]
    assert row == ("Apple Inc", "US", "Technology")


def test_seeded_dimensions_are_found_not_duplicated(pipeline):
    """01_schema.sql seeds these; the helpers must reuse the seeded rows."""
    for name in ("finnhub", "alphavantage", "twelvedata"):
        pipeline.db.source_id(name)
    assert pipeline.scalar("SELECT COUNT(*) FROM dim_source") == 3

    for code in ("1min", "5min", "1day", "daily"):
        pipeline.db.interval_id(code)
    assert pipeline.scalar("SELECT COUNT(*) FROM dim_interval") == 4

    for name in ("RSI", "MACD", "EMA", "SMA"):
        pipeline.db.indicator_id(name)
    assert pipeline.scalar("SELECT COUNT(*) FROM dim_indicator") == 4


def test_bi_dim_symbol_shows_the_real_company(pipeline):
    """End of the chain: what Power BI actually reads."""
    pipeline.db.symbol_id("AAPL")
    pipeline.db.symbol_id("AAPL", company_name="Apple Inc", exchange="NASDAQ",
                          sector="Technology")

    row = pipeline.query(
        "SELECT company_name, exchange, sector FROM bi_dim_symbol "
        "WHERE symbol_code='AAPL'"
    )[0]
    assert row == ("Apple Inc", "NASDAQ", "Technology")
    assert "UNKNOWN" not in row and "Uncategorized" not in row
