#!/usr/bin/env python3
"""
One-way replication between two copies of this schema.

    Render Postgres  ->  the Postgres on your own machine      (keep a copy)
    your machine     ->  a brand new Render Postgres           (rebuild it)

Same script both ways: it only ever reads from --source and writes to
--target, so swapping the two URLs is what turns a backup into a restore.
That is the whole answer to "Render deletes the free database after 30
days": the copy on your machine becomes the source, a new empty database
becomes the target, and nothing is lost.

    # keep a local copy fresh, forever
    python tools/replicate.py --source "$RENDER_URL" --target "$LOCAL_URL" \
        --loop 10

    # one pass, and say what it would do without writing
    python tools/replicate.py --source A --target B --once --dry-run

    # rebuild a new Render database from the local copy
    python tools/replicate.py --source "$LOCAL_URL" --target "$NEW_RENDER_URL" --full


WHY THIS IS NOT A FILE EXPORT
-----------------------------
A CSV or an .xlsx snapshot has to be regenerated whole every time, and
loading it means replacing what is already there. This copies only the rows
the target has not seen, so a pass costs a few small queries when nothing
changed, which is what makes a short interval affordable at all. The file
exports still exist (tools/export_data.py) for the cases a file is the
right answer -- opening the data in Excel, or having something to download
when neither database is reachable.


THE PART THAT IS EASY TO GET WRONG
----------------------------------
symbol_id, source_id, interval_id and indicator_id are SERIAL columns, so
the same symbol can be id 2 in one database and id 7 in the other. And it
will be: 01_schema.sql SEEDS dim_source, dim_interval and dim_indicator, so
a freshly created target already holds those rows under ids it assigned
itself.

Copying a fact row's raw symbol_id into such a database therefore attaches
the price to the wrong company -- quietly, with no error, because the id
exists and the foreign key is satisfied. Every number in the report is then
wrong in a way no row count reveals.

So dimensions are matched on their NATURAL key (symbol_code, source_name,
interval_code, indicator_name), which produces a remote-id -> local-id map,
and every fact's foreign keys are translated through that map before the
row is written. Surrogate ids are never copied.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tools import pgconnect  # noqa: E402

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Berlin")

try:
    ZONE: timezone | ZoneInfo = ZoneInfo(APP_TIMEZONE)
    ZONE_LABEL = APP_TIMEZONE
except Exception:
    # Windows ships no IANA database, so this is the normal path there
    # unless `tzdata` is installed. Falling back to UTC *while still
    # printing "Europe/Berlin"* puts a two-hour error under the right
    # label, which is worse than an honest one. The system's own zone is
    # the better guess, and the label says what was actually used.
    _now = datetime.now().astimezone()
    ZONE = _now.tzinfo or timezone.utc
    ZONE_LABEL = (f"{_now.tzname() or 'local time'} (system clock; "
                  f"pip install tzdata for {APP_TIMEZONE})")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
# staticmethod, not a bare lambda: assigning a plain function to the class
# makes it bind, so logging would call it as converter(self, secs) and the
# timestamp would never render. logging swallows formatting errors to
# stderr instead of raising, so the bare version fails silently -- the log
# line simply loses its clock and nothing says why.
logging.Formatter.converter = staticmethod(
    lambda secs: datetime.fromtimestamp(secs, ZONE).timetuple()
)
log = logging.getLogger("replicate")


# ---------------------------------------------------------------- the plan
@dataclass(frozen=True)
class Dimension:
    table: str
    pk: str
    natural: str             # the column that identifies the row for real
    columns: tuple[str, ...]  # everything except the surrogate pk


@dataclass(frozen=True)
class Fact:
    table: str
    pk: str
    columns: tuple[str, ...]
    # Which of this table's columns point at which dimension. Translated,
    # never copied.
    fks: dict[str, str] = field(default_factory=dict)
    # ON CONFLICT target. None means the table has no unique constraint, so
    # the watermark is the ONLY thing preventing duplicates -- which is why
    # sync_fact refuses to honour --full for such a table.
    conflict: tuple[str, ...] | None = None
    # Re-read this many rows below the watermark on every pass, so a row
    # updated in place after it was first copied is picked up again.
    # log_pipeline_run is the reason this exists: a run is INSERTed as
    # "running" and UPDATEd to "success" minutes later, and without an
    # overlap the copy would keep every run stuck at "running" forever.
    overlap: int = 0


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("dim_source", "source_id", "source_name",
              ("source_name", "base_url", "notes")),
    Dimension("dim_interval", "interval_id", "interval_code",
              ("interval_code", "interval_type")),
    Dimension("dim_indicator", "indicator_id", "indicator_name",
              ("indicator_name", "description", "category")),
    Dimension("dim_symbol", "symbol_id", "symbol_code",
              ("symbol_code", "company_name", "exchange", "country",
               "currency", "sector", "industry")),
)

FACTS: tuple[Fact, ...] = (
    Fact(
        "fact_market_timeseries", "timeseries_id",
        ("symbol_id", "source_id", "interval_id", "candle_time_utc",
         "open", "high", "low", "close", "volume", "raw_payload"),
        fks={"symbol_id": "dim_symbol", "source_id": "dim_source",
             "interval_id": "dim_interval"},
        conflict=("symbol_id", "interval_id", "candle_time_utc"),
    ),
    Fact(
        "fact_market_quote", "quote_id",
        ("symbol_id", "source_id", "fetched_at_utc", "quote_time_utc",
         "price", "open", "high", "low", "previous_close", "change",
         "change_pct", "raw_payload"),
        fks={"symbol_id": "dim_symbol", "source_id": "dim_source"},
        # quote_time_utc is nullable, and ON CONFLICT never matches a NULL
        # (NULL = NULL is unknown, not true). So for a quote with no
        # provider timestamp this clause simply does not fire, and the
        # watermark is what prevents a second copy. That is why the
        # watermark is the remote primary key and not a timestamp.
        conflict=("symbol_id", "source_id", "quote_time_utc"),
    ),
    Fact(
        "fact_market_indicator", "indicator_fact_id",
        ("symbol_id", "source_id", "indicator_id", "interval_id",
         "candle_time_utc", "value", "macd", "macd_signal", "macd_hist",
         "raw_payload"),
        fks={"symbol_id": "dim_symbol", "source_id": "dim_source",
             "indicator_id": "dim_indicator", "interval_id": "dim_interval"},
        conflict=("symbol_id", "indicator_id", "interval_id", "candle_time_utc"),
    ),
    Fact(
        "fact_company_fundamental", "fundamental_id",
        ("symbol_id", "source_id", "fetched_at_utc", "fetched_date",
         "ipo_date", "market_cap", "share_outstanding", "pe_ratio", "eps_ttm",
         "gross_margin", "net_margin", "roe", "debt_to_equity",
         "current_ratio", "beta", "week_52_high", "week_52_low",
         "raw_profile", "raw_metrics"),
        fks={"symbol_id": "dim_symbol", "source_id": "dim_source"},
        conflict=("symbol_id", "source_id", "fetched_date"),
        overlap=20,
    ),
    Fact(
        "fact_earnings_calendar", "earnings_id",
        ("symbol_id", "source_id", "fetched_at_utc", "report_date", "hour",
         "eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual",
         "raw_payload"),
        fks={"symbol_id": "dim_symbol", "source_id": "dim_source"},
        conflict=("symbol_id", "report_date"),
        # An estimate becomes an actual when the company reports, so these
        # rows change after they are first written.
        overlap=50,
    ),
    Fact(
        "log_pipeline_run", "run_id",
        ("run_uid", "trigger_source", "started_at_utc", "finished_at_utc",
         "duration_seconds", "status", "symbols_total", "symbols_ok",
         "symbols_failed", "rows_written", "error_msg"),
        conflict=("run_uid",),
        overlap=10,
    ),
    Fact(
        "log_api_call", "log_id",
        ("source_id", "symbol_id", "called_at_utc", "endpoint",
         "http_status", "response_ms", "error_msg"),
        fks={"source_id": "dim_source", "symbol_id": "dim_symbol"},
        # No unique constraint on this table, by design: every attempt is
        # logged, including retries, so two identical-looking rows are two
        # real events and must both survive. Nothing to conflict on, so the
        # watermark alone keeps the copy exact.
        conflict=None,
        # run_id is deliberately NOT copied. It points at log_pipeline_run's
        # surrogate key in the SOURCE database, which means nothing here;
        # keeping it would be the same class of bug as copying symbol_id.
    ),
)

# The script's own bookkeeping. Not part of the replicated schema, so the
# target still holds exactly the tables 01_schema.sql creates plus this one.
STATE_DDL = """
CREATE TABLE IF NOT EXISTS replication_state (
    table_name      TEXT PRIMARY KEY,
    last_source_id  BIGINT NOT NULL DEFAULT 0,
    rows_copied     BIGINT NOT NULL DEFAULT 0,
    last_pass_at    TIMESTAMPTZ,
    source_label    TEXT
)
"""


# ------------------------------------------------------------------ helpers
def connect(url: str, label: str, read_only: bool = False):
    # Through pgconnect: a German or French PostgreSQL reports a refused
    # password in its own language, and psycopg2 decodes that as UTF-8 and
    # raises UnicodeDecodeError instead. The reason never reaches you.
    try:
        conn = pgconnect.connect_raw(
            url, timeout=15, options=f"-c TimeZone={APP_TIMEZONE}"
        )
    except pgconnect.ConnectionFailed as exc:
        raise SystemExit(f"Cannot reach the {label} database:\n  {exc}") from exc
    conn.autocommit = False
    if read_only:
        # Belt and braces: the source is only ever read, and the server
        # refusing a write is a stronger guarantee than remembering not to.
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
        conn.commit()
    return conn


def json_columns(conn) -> dict[str, set[str]]:
    """
    Which columns are json or jsonb, per table.

    psycopg2 hands a jsonb column back as a Python dict (or list), and
    passing that straight into the next INSERT fails with "can't adapt type
    'dict'" -- the value has to be wrapped again on the way in.

    Discovered rather than listed, so a jsonb column added to the schema
    later is handled without anyone remembering to update a list here. The
    alternative, registering a global dict adapter, would change how
    psycopg2 behaves for every other query in the process.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND data_type IN ('json', 'jsonb')
        """)
        out: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            out.setdefault(table, set()).add(column)
    conn.commit()
    return out


def schema_check(conn, label: str) -> None:
    """Fail clearly now rather than obscurely on the first INSERT."""
    wanted = [d.table for d in DIMENSIONS] + [f.table for f in FACTS]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            " WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (wanted,),
        )
        found = {r[0] for r in cur.fetchall()}
    missing = [t for t in wanted if t not in found]
    if missing:
        raise SystemExit(
            f"The {label} database is missing {len(missing)} table(s):\n"
            f"  {', '.join(missing)}\n\n"
            f"Create the schema there first:\n"
            f"  psql \"<{label} url>\" -f sql/01_schema.sql\n"
            f"  psql \"<{label} url>\" -f sql/02_bi_views.sql"
        )


def read_state(target) -> dict[str, int]:
    with target.cursor() as cur:
        cur.execute(STATE_DDL)
        cur.execute("SELECT table_name, last_source_id FROM replication_state")
        rows = cur.fetchall()
    target.commit()
    return {t: i for t, i in rows}


def save_state(target, table: str, last_id: int, copied: int, source_label: str) -> None:
    with target.cursor() as cur:
        cur.execute(
            """
            INSERT INTO replication_state
                   (table_name, last_source_id, rows_copied, last_pass_at, source_label)
            VALUES (%s, %s, %s, NOW(), %s)
            ON CONFLICT (table_name) DO UPDATE
               SET last_source_id = EXCLUDED.last_source_id,
                   rows_copied    = replication_state.rows_copied + EXCLUDED.rows_copied,
                   last_pass_at   = EXCLUDED.last_pass_at,
                   source_label   = EXCLUDED.source_label
            """,
            (table, last_id, copied, source_label),
        )
    target.commit()


# --------------------------------------------------------------- dimensions
def sync_dimensions(source, target, dry_run: bool) -> dict[str, dict[int, int]]:
    """
    Copy the dimensions and return, per dimension, source id -> target id.

    Small tables, so they are copied whole every pass: a renamed company or
    a newly filled-in sector has to reach the copy, and reading four tables
    of a handful of rows each costs nothing next to the fact tables.
    """
    maps: dict[str, dict[int, int]] = {}

    for dim in DIMENSIONS:
        cols = ", ".join(dim.columns)
        with source.cursor() as cur:
            cur.execute(f"SELECT {dim.pk}, {cols} FROM {dim.table}")
            rows = cur.fetchall()

        mapping: dict[int, int] = {}
        inserted = 0

        for row in rows:
            source_id, values = row[0], row[1:]
            if dry_run:
                # Still resolve what the id WOULD be, so a dry run reports
                # realistic numbers instead of pretending nothing matches.
                with target.cursor() as cur:
                    cur.execute(
                        f"SELECT {dim.pk} FROM {dim.table} WHERE {dim.natural} = %s",
                        (values[dim.columns.index(dim.natural)],),
                    )
                    found = cur.fetchone()
                if found:
                    mapping[source_id] = found[0]
                else:
                    inserted += 1
                continue

            updates = [c for c in dim.columns if c != dim.natural]
            # COALESCE on the way in: a later pass whose provider left a
            # field blank must not erase a value an earlier pass had
            # already captured. Without this, one profile call that
            # returned no sector would blank the sector in the copy.
            set_clause = ", ".join(
                f"{c} = COALESCE(EXCLUDED.{c}, {dim.table}.{c})" for c in updates
            )
            with target.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {dim.table} ({cols})
                    VALUES ({', '.join(['%s'] * len(dim.columns))})
                    ON CONFLICT ({dim.natural}) DO UPDATE SET {set_clause}
                    RETURNING {dim.pk}
                    """,
                    values,
                )
                mapping[source_id] = cur.fetchone()[0]
            inserted += 1

        if not dry_run:
            target.commit()
        maps[dim.table] = mapping
        log.info("%-22s %d row(s)", dim.table, len(rows))

    return maps


# -------------------------------------------------------------------- facts
def sync_fact(source, target, fact: Fact, maps: dict[str, dict[int, int]],
              state: dict[str, int], batch: int, dry_run: bool,
              source_label: str, full: bool,
              jsonb: set[str] | None = None) -> tuple[int, int]:
    """Copy new rows of one fact table. Returns (read, written)."""
    if full and fact.conflict is None:
        # --full works by re-reading from zero and letting ON CONFLICT make
        # the repeat harmless. This table has no unique constraint to
        # conflict on, so a second read inserts every row a SECOND TIME --
        # measured: 39 api-call rows became 78, and the dashboard's success
        # rate and the Power BI api view are computed from them.
        #
        # So --full is ignored here and the stored watermark stands. The
        # one case that cannot be recovered this way is a target that holds
        # rows while the state row that says how far it got has been lost;
        # that is reported rather than guessed at, because guessing wrong
        # means silent duplicates.
        watermark = state.get(fact.table, 0)
        if watermark == 0:
            with target.cursor() as cur:
                cur.execute(f"SELECT EXISTS (SELECT 1 FROM {fact.table})")
                if cur.fetchone()[0]:
                    log.warning(
                        "%-26s skipped: --full would duplicate it. %s has no "
                        "unique key and no watermark is stored. To rebuild it: "
                        "TRUNCATE %s, then run again.",
                        fact.table, fact.table, fact.table)
                    return 0, 0
    else:
        watermark = 0 if full else max(0, state.get(fact.table, 0) - fact.overlap)
    cols = ", ".join(fact.columns)
    placeholders = ", ".join(["%s"] * len(fact.columns))
    read = written = 0
    highest = state.get(fact.table, 0)
    json_idx = [fact.columns.index(c) for c in (jsonb or set())
                if c in fact.columns]

    while True:
        with source.cursor() as cur:
            cur.execute(
                f"SELECT {fact.pk}, {cols} FROM {fact.table} "
                f" WHERE {fact.pk} > %s ORDER BY {fact.pk} LIMIT %s",
                (watermark, batch),
            )
            rows = cur.fetchall()
        if not rows:
            break

        read += len(rows)
        watermark = rows[-1][0]
        highest = max(highest, watermark)

        if dry_run:
            continue

        payload = []
        skipped = 0
        for row in rows:
            values = list(row[1:])
            broken = False
            for col, dim_table in fact.fks.items():
                i = fact.columns.index(col)
                source_id = values[i]
                if source_id is None:
                    continue  # symbol_id on log_api_call is nullable
                mapped = maps[dim_table].get(source_id)
                if mapped is None:
                    # The source has a fact pointing at a dimension row the
                    # source itself no longer has, or one added between the
                    # dimension pass and now. Dropping the row is right:
                    # writing it with an unmapped id would attach it to
                    # whatever company happens to hold that id in the target.
                    broken = True
                    break
                values[i] = mapped
            if broken:
                skipped += 1
                continue
            # Re-wrap what came back as a dict or list so it goes in as
            # jsonb rather than failing to adapt.
            for i in json_idx:
                if values[i] is not None:
                    values[i] = psycopg2.extras.Json(values[i])
            payload.append(values)

        if skipped:
            log.warning("%s: skipped %d row(s) whose dimension is missing in "
                        "the source", fact.table, skipped)

        if payload:
            if fact.conflict:
                updates = [c for c in fact.columns if c not in fact.conflict]
                set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
                sql = (
                    f"INSERT INTO {fact.table} ({cols}) VALUES %s "
                    f"ON CONFLICT ({', '.join(fact.conflict)}) "
                    f"DO UPDATE SET {set_clause}"
                )
            else:
                sql = f"INSERT INTO {fact.table} ({cols}) VALUES %s"

            with target.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur, sql, payload, template=f"({placeholders})",
                    page_size=batch,
                )
            written += len(payload)

        target.commit()
        if len(rows) < batch:
            break

    if not dry_run and (written or highest != state.get(fact.table, 0)):
        save_state(target, fact.table, highest, written, source_label)
        state[fact.table] = highest

    return read, written


def fix_sequences(target, dry_run: bool) -> None:
    """
    Point every sequence past the largest id present.

    Rows are written with explicit ids only for the dimensions' natural-key
    upserts; the surrogate ids come from the target's own sequences. But a
    rebuild that restored from a dump, or a sequence left behind by a
    truncate, can leave a sequence below the data -- and then the next
    insert fails on a duplicate key. One cheap statement removes a whole
    class of confusing failure.
    """
    if dry_run:
        return
    pairs = [(d.table, d.pk) for d in DIMENSIONS] + [(f.table, f.pk) for f in FACTS]
    with target.cursor() as cur:
        for table, pk in pairs:
            cur.execute(
                "SELECT setval(pg_get_serial_sequence(%s, %s), "
                "       COALESCE((SELECT MAX(" + pk + ") FROM " + table + "), 1), "
                "       (SELECT COUNT(*) FROM " + table + ") > 0)",
                (table, pk),
            )
    target.commit()


# --------------------------------------------------------------------- pass
def one_pass(source_url: str, target_url: str, *, batch: int, dry_run: bool,
             full: bool, source_label: str) -> dict[str, tuple[int, int]]:
    source = connect(source_url, "source", read_only=True)
    target = connect(target_url, "target")
    try:
        schema_check(source, "source")
        schema_check(target, "target")

        state = read_state(target)
        jsonb = json_columns(target)
        maps = sync_dimensions(source, target, dry_run)

        totals: dict[str, tuple[int, int]] = {}
        for fact in FACTS:
            read, written = sync_fact(
                source, target, fact, maps, state, batch, dry_run,
                source_label, full, jsonb.get(fact.table, set()),
            )
            totals[fact.table] = (read, written)
            if read:
                log.info("%-26s read %6d  wrote %6d", fact.table, read, written)

        fix_sequences(target, dry_run)
        return totals
    finally:
        source.close()
        target.close()


def _label(url: str) -> str:
    """host/dbname for the log and for replication_state. See pgconnect."""
    return pgconnect.safe_label(url)


# ---------------------------------------------------------------------
# KEEPING THE DASHBOARD AWAKE
#
# A free Render WEB SERVICE spins down after 15 minutes without traffic
# and takes about a minute to wake. The DATABASE does not: Render's free
# plan spins down web services only, so nothing below affects whether the
# data keeps flowing. This is about the status page loading quickly.
#
# There is a keepalive workflow in GitHub Actions that does the same job,
# and this exists because that one cannot be relied on: GitHub states
# plainly that scheduled runs are best-effort and may be delayed or
# dropped under load, and on this repository most of them are. A loop
# already running on your own machine is not best-effort.
#
# TWO LIMITS SHAPE THIS, and ignoring either one costs money:
#
#   750 instance-hours per month on the free plan. A service kept awake
#   around the clock uses about 744 -- the whole allowance for one
#   service, with nothing left over. So pinging is confined to a daytime
#   window (~465 hours/month), the same one the workflow uses.
#
#   --loop 1 means 86,400 passes a day. A ping on each would be pointless
#   as well as rude: 15 minutes of silence is what spins the service
#   down, so once every 10 minutes is enough to prevent it, whatever the
#   loop interval.
# ---------------------------------------------------------------------
PING_EVERY_SECONDS = 600
PING_HOURS_UTC = range(6, 21)


class KeepAwake:
    """Pings a URL occasionally. Never raises, never fails a pass."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._last = 0.0

    def maybe_ping(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if datetime.now(timezone.utc).hour not in PING_HOURS_UTC:
            return False
        if now - self._last < PING_EVERY_SECONDS:
            return False
        self._last = now
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=20) as r:
                log.debug("keep-awake: %s returned %s", self.url, r.status)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Waking a sleeping instance regularly takes longer than the
            # timeout, and the request that timed out still woke it. A
            # failure here is never a reason to stop replicating.
            log.debug("keep-awake: %s", exc)
        return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Copy rows from one copy of this schema to another.",
    )
    ap.add_argument("--source", default=os.getenv("SOURCE_DATABASE_URL", ""),
                    help="read from here (default: $SOURCE_DATABASE_URL)")
    ap.add_argument("--target", default=os.getenv("LOCAL_DATABASE_URL", ""),
                    help="write to here (default: $LOCAL_DATABASE_URL)")
    ap.add_argument("--loop", type=float, metavar="SECONDS",
                    help="keep going, waiting this long between passes")
    ap.add_argument("--once", action="store_true", help="a single pass (default)")
    ap.add_argument("--full", action="store_true",
                    help="ignore the watermark and re-read everything")
    ap.add_argument("--batch", type=int, default=5000,
                    help="rows per round trip (default 5000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be copied, write nothing")
    ap.add_argument("--keep-awake", metavar="URL",
                    default=os.getenv("DASHBOARD_URL", ""),
                    help="with --loop, also ping this dashboard every 10 min "
                         "during the day so it does not spin down "
                         "(default: $DASHBOARD_URL)")
    args = ap.parse_args(argv)

    if not args.source or not args.target:
        ap.error(
            "both --source and --target are required.\n"
            "  --source  the Render connection string (its External URL)\n"
            "  --target  your own Postgres, e.g.\n"
            "            postgresql://postgres:PASSWORD@localhost:5432/marketdb\n"
            "Or set SOURCE_DATABASE_URL and LOCAL_DATABASE_URL."
        )
    if args.source == args.target:
        ap.error("--source and --target are the same database.")

    # Before connecting, not after. libpq's report of a connection string
    # that contains itself is a complaint about a port nobody typed, and
    # it names neither the file nor the fix. See complain_about_url.
    for which, url in (("--source", args.source), ("--target", args.target)):
        wrong = pgconnect.complain_about_url(url)
        if wrong:
            log.error("%s is not usable: %s", which, wrong)
            return 2

    label = _label(args.source)
    log.info("source  %s", label)
    log.info("target  %s", _label(args.target))
    log.info("clock   %s (%s)", ZONE_LABEL,
             datetime.now(ZONE).strftime("%Y-%m-%d %H:%M:%S %Z"))
    if args.dry_run:
        log.info("dry run: nothing will be written")

    keep_awake = None
    if args.keep_awake:
        if not args.loop:
            log.info("--keep-awake needs --loop; ignoring it for a single pass")
        else:
            keep_awake = KeepAwake(args.keep_awake)
            log.info("awake   %s every %d min, %02d:00-%02d:59 UTC",
                     keep_awake.url, PING_EVERY_SECONDS // 60,
                     PING_HOURS_UTC[0], PING_HOURS_UTC[-1])

    full = args.full
    passes = 0
    while True:
        started = time.perf_counter()
        if keep_awake is not None:
            keep_awake.maybe_ping()
        try:
            totals = one_pass(args.source, args.target, batch=args.batch,
                              dry_run=args.dry_run, full=full,
                              source_label=label)
        except SystemExit:
            raise
        except Exception as exc:
            # A loop must survive the source going away. Not because the
            # database sleeps -- Render spins down free WEB SERVICES, not
            # free databases -- but because a long-running connection over
            # the public internet drops sooner or later, and a laptop lid
            # closes. One pass is lost; the next catches up, because the
            # watermark is stored in the target, not held in memory here.
            log.error("pass failed: %s", exc)
            if not args.loop:
                return 1
            time.sleep(args.loop)
            continue

        passes += 1
        full = False  # --full applies to the first pass only
        wrote = sum(w for _r, w in totals.values())
        took = time.perf_counter() - started
        if wrote:
            log.info("pass %d: %d row(s) in %.1fs", passes, wrote, took)
        else:
            log.info("pass %d: up to date (%.1fs)", passes, took)

        if not args.loop:
            return 0
        time.sleep(max(0.0, args.loop - took))


if __name__ == "__main__":
    sys.exit(main())
