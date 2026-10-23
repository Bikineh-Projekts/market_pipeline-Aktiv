"""
One interval, one code.

Alpha Vantage calls the daily interval "daily"; Twelve Data calls it
"1day". Each collector wrote whatever string it had just sent to its API
straight into dim_interval, and 01_schema.sql seeded both codes, so one
interval existed as two rows with two ids: the daily candles under
"1day", the daily indicators under "daily".

Nothing failed. That is why it survived long enough to reach production
and a Power BI model. bi_fact_price_daily had already been taught to
accept either code -- the symptom patched in one view, the split left in
the data underneath. It only bites on a join:

    price JOIN indicator ON symbol + date                -> every row
    price JOIN indicator ON symbol + date + interval_id  -> ZERO rows

with no error anywhere. In Power BI that is an empty visual, and an empty
visual reads as "nothing has been collected yet", not as a broken join.

These tests are about the two places that produced it: the seed, and the
collector that let its provider's vocabulary reach the warehouse.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Each provider's word for an interval -> the one code we store. Two keys
# pointing at the same value is the whole bug in one line.
ALIASES = {"daily": "1day", "weekly": "1week", "monthly": "1month"}


def seeded_interval_codes() -> list[str]:
    schema = (REPO / "sql" / "01_schema.sql").read_text()
    block = re.search(
        r"INSERT INTO dim_interval[^;]*?VALUES(.*?);", schema, re.S | re.I)
    assert block, "no dim_interval seed found in 01_schema.sql"
    return re.findall(r"\('([^']+)'\s*,", block.group(1))


def test_the_schema_seeds_no_two_codes_for_one_interval():
    """
    The seed is re-applied by the migrate action. While 'daily' was
    seeded here, every migrate put the alias straight back -- so fixing
    the collector alone would have been undone on the next deploy.
    """
    codes = seeded_interval_codes()
    offenders = [c for c in codes if c in ALIASES]
    assert not offenders, (
        f"01_schema.sql seeds {offenders}, which mean the same as "
        f"{[ALIASES[c] for c in offenders]}. Seed one code per interval."
    )


def test_the_seeded_codes_are_unique():
    codes = seeded_interval_codes()
    assert len(codes) == len(set(codes)), f"duplicate codes seeded: {codes}"


def test_the_alphavantage_collector_maps_its_providers_word():
    """
    The API parameter and the dimension value were one string doing two
    jobs. They are allowed to differ, and for the daily interval they do.
    """
    source = (REPO / "pipeline" / "collectors" / "alphavantage.py").read_text()
    assert "CANONICAL_INTERVAL" in source, "the mapping is gone"
    assert re.search(r"db\.interval_id\(\s*CANONICAL_INTERVAL", source), (
        "alphavantage writes its raw API interval into dim_interval again; "
        "it must go through CANONICAL_INTERVAL first"
    )


@pytest.mark.parametrize("provider_word,stored", sorted(ALIASES.items()))
def test_the_mapping_covers_every_alias(provider_word, stored):
    from pipeline.collectors.alphavantage import CANONICAL_INTERVAL

    assert CANONICAL_INTERVAL.get(provider_word) == stored


def test_an_unknown_interval_is_stored_under_its_own_name():
    """
    Not dropped, and not quietly filed under the wrong interval. A new
    interval appearing as itself is visible; a new interval silently
    merged into the daily series is the bug again, upside down.
    """
    from pipeline.collectors.alphavantage import CANONICAL_INTERVAL

    assert CANONICAL_INTERVAL.get("4hour", "4hour") == "4hour"


def test_the_migration_is_a_single_transaction():
    """
    It moves fact rows between dimension ids. Half of that applied is
    worse than none of it, so it must be all-or-nothing.
    """
    sql = (REPO / "sql" / "08_merge_duplicate_intervals.sql").read_text()
    assert re.search(r"^BEGIN;", sql, re.M), "migration does not open a transaction"
    assert re.search(r"^COMMIT;", sql, re.M), "migration does not commit"


def test_the_migration_handles_the_unique_constraint():
    """
    Moving a row onto the canonical id can collide with a row already
    there. A bare UPDATE aborts the whole migration on the first clash.
    """
    sql = (REPO / "sql" / "08_merge_duplicate_intervals.sql").read_text()
    assert "EXISTS" in sql and "DELETE FROM fact_market_indicator" in sql, (
        "the migration must remove rows that would collide before moving the rest"
    )
