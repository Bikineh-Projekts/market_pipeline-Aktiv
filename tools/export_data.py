#!/usr/bin/env python3
"""
Export the database to files: CSV for loading, one .xlsx for looking at.

    python tools/export_data.py --url "$DATABASE_URL" --out export/

Produces, under --out:

    csv/<table>.csv.gz   every raw table, gzipped. This is what a bulk
                         load wants: `\\copy ... FROM PROGRAM 'gzip -dc f'`
                         is orders of magnitude faster than row-by-row
                         INSERTs, and CSV has no row limit.
    market_data.xlsx     the bi_* views, one sheet each, for opening in
                         Excel. Capped -- see below.
    manifest.json        row counts and timestamps, so a consumer can tell
                         whether anything changed without parsing the data.
    README.md            what the files are and how to load them.


WHEN TO USE THIS, AND WHEN NOT TO
---------------------------------
Files are a snapshot. Every export regenerates all of them, and loading one
means replacing what is already there, so the cost grows with the history
rather than with what changed. That makes them the right answer for three
things -- opening the data in Excel, keeping something downloadable when
neither database is reachable, and seeding a brand new database -- and the
wrong answer for keeping a copy continuously up to date.

For that, tools/replicate.py copies only the rows the other side has not
seen, which costs a few small queries when nothing has changed. Measured:
about 35 ms against a database on the same machine.


THE EXCEL ROW LIMIT IS REAL
---------------------------
A worksheet holds 1,048,576 rows, and that is a hard format limit, not a
setting. A sheet that would exceed it is truncated to the NEWEST rows and
the truncation is recorded in the sheet, in the manifest and on stdout --
silently dropping the oldest history would be the worst of the three
possible behaviours. The CSV files are never truncated.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

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

# A worksheet cannot hold more than this. Not a tunable.
EXCEL_MAX_ROWS = 1_048_576
SHEET_LIMIT = EXCEL_MAX_ROWS - 2  # leave room for the header and a note

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("export")

# Raw tables, in an order a restore can replay: dimensions before the facts
# that reference them.
TABLES = (
    "dim_source", "dim_interval", "dim_indicator", "dim_symbol",
    "fact_market_timeseries", "fact_market_quote", "fact_market_indicator",
    "fact_company_fundamental", "fact_earnings_calendar",
    "log_pipeline_run", "log_api_call",
)

# Which views go into the workbook, and how each one is ordered. Newest
# first, so a truncated sheet keeps the rows someone actually wants.
VIEWS: tuple[tuple[str, str], ...] = (
    ("bi_dim_symbol", "symbol_code"),
    ("bi_dim_date", "date_key DESC"),
    ("bi_fact_price_daily", "trade_date DESC, symbol_id"),
    ("bi_fact_price_intraday", "candle_time_utc DESC, symbol_id"),
    ("bi_fact_indicator_daily", "trade_date DESC, symbol_id"),
    ("bi_fact_quote_latest", "symbol_id"),
    ("bi_fact_quote_history", "fetched_at_utc DESC"),
    ("bi_fact_fundamental_latest", "symbol_id"),
    ("bi_fact_earnings", "report_date DESC"),
    ("bi_fact_pipeline_run", "started_at_utc DESC"),
    ("bi_fact_api_call", "called_at_utc DESC"),
    ("bi_pipeline_health", "1"),
    ("bi_storage_usage", "bytes DESC"),
)


def _cell(value, column: str = ""):
    """
    Everything Excel and CSV can hold, and nothing they cannot.

    `column` decides which clock a timestamp is written on, and it is not
    cosmetic. psycopg2 returns a TIMESTAMPTZ as an aware datetime, and
    openpyxl cannot store an aware datetime at all -- so it has to be made
    naive, which means choosing a zone. Choosing the display zone for
    every column wrote the Berlin time into `called_at_utc`, i.e. a column
    whose name promised UTC held local time, which is worse than either
    one consistently.

    So the suffix is honoured: `*_utc` is written in UTC, everything else
    in the display zone.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list)):
        # jsonb. Excel shows a Record placeholder for structured data and
        # Power BI cannot use it either, so it travels as compact JSON text.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            zone = timezone.utc if column.endswith("_utc") else ZONE
            value = value.astimezone(zone).replace(tzinfo=None)
        return value
    return value


def _row(values, header: list[str]) -> list:
    return [_cell(v, c) for v, c in zip(values, header)]


def _header_of(conn, relation: str) -> list[str]:
    """
    Column names, from a plain cursor.

    A named (server-side) cursor leaves `description` as None until rows
    have actually been fetched, so asking it for the header before the
    first fetch raises -- and for an empty table it would never be
    populated at all. A LIMIT 0 on an ordinary cursor always answers.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {relation} LIMIT 0")
        return [d.name for d in cur.description]


def _connect(url: str):
    conn = psycopg2.connect(url, connect_timeout=20,
                            options=f"-c TimeZone={APP_TIMEZONE}")
    with conn.cursor() as cur:
        # An export must never be able to change anything it is exporting.
        cur.execute("SET default_transaction_read_only = on")
    conn.commit()
    return conn


def export_csv(conn, out: pathlib.Path) -> dict[str, int]:
    """Every raw table, gzipped, uncapped."""
    folder = out / "csv"
    folder.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    for table in TABLES:
        path = folder / f"{table}.csv.gz"
        header = _header_of(conn, table)
        with conn.cursor(name=f"cur_{table}") as cur:
            # A named (server-side) cursor streams instead of loading the
            # whole table into memory. fact_market_timeseries is the one
            # that grows without limit, and a free instance has little RAM.
            cur.itersize = 10_000
            cur.execute(f"SELECT * FROM {table}")
            rows = 0
            with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(header)
                for row in cur:
                    writer.writerow(_row(row, header))
                    rows += 1
        counts[table] = rows
        log.info("csv/%-32s %8d rows  %6.1f KB",
                 f"{table}.csv.gz", rows, path.stat().st_size / 1024)
    return counts


def export_xlsx(conn, out: pathlib.Path) -> tuple[dict[str, int], list[str]]:
    """The BI views as one workbook. Returns (counts, truncated sheets)."""
    try:
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font
    except ImportError:
        raise SystemExit(
            "openpyxl is needed to write the .xlsx.\n"
            "  pip install openpyxl\n"
            "The CSV export does not need it."
        )

    book = Workbook(write_only=True)
    counts: dict[str, int] = {}
    truncated: list[str] = []
    bold = Font(bold=True)

    for view, order in VIEWS:
        with conn.cursor() as probe:
            probe.execute(
                "SELECT 1 FROM information_schema.views "
                " WHERE table_schema='public' AND table_name=%s", (view,))
            if probe.fetchone() is None:
                log.warning("%s is not in this database; skipped", view)
                continue

        # Excel sheet names are capped at 31 characters, and the bi_ prefix
        # is noise once every sheet has it.
        sheet = book.create_sheet(view.removeprefix("bi_")[:31])

        header = _header_of(conn, view)
        with conn.cursor(name=f"x_{view}") as cur:
            cur.itersize = 5_000
            cur.execute(f"SELECT * FROM {view} ORDER BY {order} "
                        f"LIMIT {SHEET_LIMIT + 1}")
            # A write-only worksheet streams rows straight out and has no
            # addressable cells, so styling has to be attached to the cell
            # object before it is appended.
            head = []
            for name in header:
                cell = WriteOnlyCell(sheet, value=name)
                cell.font = bold
                head.append(cell)
            sheet.append(head)
            rows = 0
            for row in cur:
                if rows >= SHEET_LIMIT:
                    truncated.append(view)
                    sheet.append([
                        f"TRUNCATED: this sheet holds the {SHEET_LIMIT:,} newest "
                        f"rows. A worksheet cannot hold more. The full data is "
                        f"in csv/{view}.csv.gz and in the database."
                    ])
                    break
                sheet.append(_row(row, header))
                rows += 1

        counts[view] = rows
        log.info("xlsx %-34s %8d rows%s", view, rows,
                 "  (TRUNCATED)" if view in truncated else "")

    path = out / "market_data.xlsx"
    out.mkdir(parents=True, exist_ok=True)
    book.save(path)
    log.info("wrote %s (%.1f MB)", path.name, path.stat().st_size / 1048576)
    return counts, truncated


def write_manifest(out: pathlib.Path, url: str, table_counts: dict[str, int],
                   view_counts: dict[str, int], truncated: list[str],
                   conn) -> dict:
    now_utc = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pg_size_pretty(pg_database_size(current_database())),
                   (SELECT MAX(fetched_at_utc)  FROM fact_market_quote),
                   (SELECT MAX(candle_time_utc) FROM fact_market_timeseries),
                   (SELECT MAX(started_at_utc)  FROM log_pipeline_run),
                   (SELECT MIN(candle_time_utc) FROM fact_market_timeseries)
        """)
        size, last_quote, last_candle, last_run, first_candle = cur.fetchone()

    def both(moment):
        if moment is None:
            return None
        return {"utc": moment.astimezone(timezone.utc).isoformat(),
                "local": moment.astimezone(ZONE).strftime("%Y-%m-%d %H:%M:%S")}

    manifest = {
        "exported_at": both(now_utc),
        "timezone": APP_TIMEZONE,
        "database_size": size,
        "source": _host(url),
        "rows": {"tables": table_counts, "views": view_counts},
        "total_rows": sum(table_counts.values()),
        "data_span": {"first_candle": both(first_candle),
                      "last_candle": both(last_candle)},
        "freshness": {"last_quote": both(last_quote), "last_run": both(last_run)},
        "truncated_sheets": truncated,
        "files": {
            "xlsx": "market_data.xlsx",
            "csv": [f"csv/{t}.csv.gz" for t in table_counts],
        },
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def _host(url: str) -> str:
    """
    The host, never the credential. This value is written into
    manifest.json, which export.yml force-pushes to the PUBLIC data-export
    branch, so it is the one label in the project that is published to the
    world. See pgconnect.safe_label for what the old version did instead.
    """
    from tools import pgconnect
    return pgconnect.safe_label(url)


README = r"""# Data export

Regenerated automatically. Every file here is replaced on each export, so
nothing in this folder is history -- it is the current contents of the
database.

| file | what it is |
| --- | --- |
| `market_data.xlsx` | the `bi_*` reporting views, one sheet each. For opening and reading. |
| `csv/*.csv.gz` | every raw table, gzipped and uncapped. For loading. |
| `manifest.json` | row counts and timestamps. Read this to tell whether anything changed. |

Every `*_local` column, and every `local` field in the manifest, is
**{tz}** wall-clock. Every `*_utc` column and `utc` field is the same
instant in UTC. Sort and compare on the UTC ones; display the local ones.

## Loading these into your own PostgreSQL

This loads into EMPTY tables -- for the first copy, or for rebuilding after
Render has deleted the database. It is **not** a way to stay up to date: it
has no idea what is already there, so running it twice fails on duplicate
keys. For that, see the replicator at the bottom.

```bash
LOCAL="postgresql://postgres:PASSWORD@localhost:5432/marketdb"

# 1. the schema, once
psql "$LOCAL" -f sql/01_schema.sql
psql "$LOCAL" -f sql/02_bi_views.sql

# 2. clear the seeded dimension rows.
#    01_schema.sql SEEDS dim_source, dim_interval and dim_indicator, so
#    those tables are NOT empty even in a brand new database, and the
#    first \copy fails on a duplicate source_name. Skipping this step is
#    the most likely reason the load below does not work.
psql "$LOCAL" -c \
  "TRUNCATE dim_source, dim_interval, dim_indicator, dim_symbol CASCADE"

# 3. the data, dimensions first -- a fact cannot reference a symbol that
#    is not there yet. This is the order manifest.json lists them in.
for t in {order}; do
  gzip -dc "csv/$t.csv.gz" \
    | psql "$LOCAL" -v ON_ERROR_STOP=1 \
        -c "\copy $t FROM STDIN WITH (FORMAT csv, HEADER true)"
done

# 4. move every sequence past the ids just loaded.
#    \copy writes the id column explicitly and does NOT advance the
#    sequence behind it, so without this the next row you insert
#    yourself collides with an existing id. That reads as a corrupt
#    database and is only a stale counter.
psql "$LOCAL" -Atc "
  SELECT format(
    'SELECT setval(%L, COALESCE((SELECT MAX(%I) FROM %I), 1), '
    'EXISTS (SELECT 1 FROM %I));',
    pg_get_serial_sequence(table_name, column_name),
    column_name, table_name, table_name)
    FROM information_schema.columns
   WHERE table_schema = 'public'
     AND pg_get_serial_sequence(table_name, column_name) IS NOT NULL
" | psql "$LOCAL" -q
```

## Keeping the copy up to date

The replicator copies only the rows the other side has not seen, so a pass
costs a few small queries when nothing changed, and running it twice
changes nothing:

```bash
python tools/replicate.py \
    --source "$RENDER_DATABASE_URL" \
    --target "$LOCAL" \
    --loop 10
```

It does step 4 for you on every pass. And because it only ever reads from
`--source` and writes to `--target`, swapping the two rebuilds a new Render
database from the local copy.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export the database to files.")
    ap.add_argument("--url", default=os.getenv("DATABASE_URL", ""),
                    help="database to read (default: $DATABASE_URL)")
    ap.add_argument("--out", default="export", help="output folder")
    ap.add_argument("--no-xlsx", action="store_true",
                    help="CSV and manifest only")
    ap.add_argument("--no-csv", action="store_true",
                    help="workbook and manifest only")
    args = ap.parse_args(argv)

    if not args.url:
        ap.error("--url is required, or set DATABASE_URL")

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    conn = _connect(args.url)
    try:
        table_counts = {} if args.no_csv else export_csv(conn, out)
        view_counts: dict[str, int] = {}
        truncated: list[str] = []
        if not args.no_xlsx:
            view_counts, truncated = export_xlsx(conn, out)

        if args.no_csv:
            # The manifest lists the raw tables either way, so a consumer
            # can always see what the database holds.
            with conn.cursor() as cur:
                for table in TABLES:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    table_counts[table] = cur.fetchone()[0]

        manifest = write_manifest(out, args.url, table_counts, view_counts,
                                  truncated, conn)
        (out / "README.md").write_text(
            README.format(tz=APP_TIMEZONE, order=" ".join(TABLES)))
    finally:
        conn.close()

    log.info("%d row(s) across %d table(s), exported to %s/",
             manifest["total_rows"], len(table_counts), out)
    if truncated:
        log.warning("%d sheet(s) hit the Excel row limit: %s",
                    len(truncated), ", ".join(truncated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
