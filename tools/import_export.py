#!/usr/bin/env python3
"""
Load the published export into your own PostgreSQL.

This is the files-to-database direction. Everything else in tools/ copies
database to database and therefore needs a live connection to Render;
this one needs only the files GitHub Actions already publishes, so it
keeps working on the day Render deletes the database.

    python tools/import_export.py --target "postgresql://postgres:PW@localhost:5432/marketdb"

With no --from it fetches the current export straight from GitHub -- no
download, no unzipping, no import dialog. --from also accepts the zip or a
folder, for a machine with no internet.

Run it as often as you like. Rows already present are not written again.

WHAT MAKES THIS MORE THAN "psql \\copy"
Two things, and both are silent when wrong rather than loud.

  The id columns. symbol_id, source_id, interval_id and indicator_id are
  SERIAL, assigned independently in each database, and 01_schema.sql SEEDS
  three of those dimensions -- so the numbers in the CSV are NOT the
  numbers in your database. Copying them across files Apple's prices under
  whatever company happens to hold that id locally. Nothing errors: the
  row counts match, every constraint holds, the audit passes, and every
  number in the report is wrong. So every id is looked up through the
  dimension's natural key instead.

  The export is a full snapshot, not a delta. Every table is written in
  full on every export, so a second import duplicates everything unless
  each row is checked. The fact tables have unique constraints and ON
  CONFLICT handles them; log_api_call deliberately has none -- every API
  attempt is logged, retries included, so two identical-looking rows are
  two real events -- and it needs the explicit guard further down.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Berlin")

# csv refuses a field over 131072 bytes by default, and raw_profile and
# raw_metrics are whole API responses stored as jsonb -- one company's
# metrics alone is well past that. Without this the import dies on
# fact_company_fundamental with "field larger than field limit", naming
# neither the table nor the column.
#
# The loop is not decoration: sys.maxsize overflows the C long that
# field_size_limit takes on Windows, which is the platform this is for.
_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_limit)
        break
    except OverflowError:
        _limit //= 2
RAW_BASE = ("https://raw.githubusercontent.com/"
            "Bikineh-Projekts/market_pipeline-Aktiv/data-export")

# Dimensions: matched on their natural key, never on the id in the file.
DIMENSIONS = {
    "dim_source":    ("source_name",    ("source_name", "base_url", "notes")),
    "dim_interval":  ("interval_code",  ("interval_code", "interval_type")),
    "dim_indicator": ("indicator_name", ("indicator_name", "description", "category")),
    "dim_symbol":    ("symbol_code",    ("symbol_code", "company_name", "exchange",
                                         "country", "currency", "sector", "industry")),
}

# (table, columns to insert, {id column: dimension}, conflict target)
FACTS = [
    ("fact_market_timeseries",
     ("symbol_id", "source_id", "interval_id", "candle_time_utc",
      "open", "high", "low", "close", "volume", "raw_payload"),
     {"symbol_id": "dim_symbol", "source_id": "dim_source",
      "interval_id": "dim_interval"},
     ("symbol_id", "interval_id", "candle_time_utc")),

    ("fact_market_quote",
     ("symbol_id", "source_id", "fetched_at_utc", "quote_time_utc", "price",
      "open", "high", "low", "previous_close", "change", "change_pct",
      "raw_payload"),
     {"symbol_id": "dim_symbol", "source_id": "dim_source"},
     ("symbol_id", "source_id", "quote_time_utc")),

    ("fact_market_indicator",
     ("symbol_id", "source_id", "indicator_id", "interval_id",
      "candle_time_utc", "value", "macd", "macd_signal", "macd_hist",
      "raw_payload"),
     {"symbol_id": "dim_symbol", "source_id": "dim_source",
      "indicator_id": "dim_indicator", "interval_id": "dim_interval"},
     ("symbol_id", "indicator_id", "interval_id", "candle_time_utc")),

    ("fact_company_fundamental",
     ("symbol_id", "source_id", "fetched_at_utc", "fetched_date", "ipo_date",
      "market_cap", "share_outstanding", "pe_ratio", "eps_ttm",
      "gross_margin", "net_margin", "roe", "debt_to_equity", "current_ratio",
      "beta", "week_52_high", "week_52_low", "raw_profile", "raw_metrics"),
     {"symbol_id": "dim_symbol", "source_id": "dim_source"},
     ("symbol_id", "source_id", "fetched_date")),

    ("fact_earnings_calendar",
     ("symbol_id", "source_id", "fetched_at_utc", "report_date", "hour",
      "eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual",
      "raw_payload"),
     {"symbol_id": "dim_symbol", "source_id": "dim_source"},
     ("symbol_id", "report_date")),

    ("log_pipeline_run",
     ("run_uid", "trigger_source", "started_at_utc", "finished_at_utc",
      "duration_seconds", "status", "symbols_total", "symbols_ok",
      "symbols_failed", "rows_written", "error_msg"),
     {},
     ("run_uid",)),

    # No conflict target: this table has no unique constraint, on purpose.
    # Handled separately, further down.
    ("log_api_call",
     ("source_id", "symbol_id", "called_at_utc", "endpoint",
      "http_status", "response_ms", "error_msg"),
     {"source_id": "dim_source", "symbol_id": "dim_symbol"},
     None),
]

ORDER = list(DIMENSIONS) + [f[0] for f in FACTS]


# ------------------------------------------------------------------ reading
class Source:
    """The export, wherever it lives. Yields decoded CSV rows per table."""

    def __init__(self, where: str | None):
        self.where = where
        self._zip = None
        if where and where.lower().endswith(".zip"):
            self._zip = zipfile.ZipFile(where)

    def label(self) -> str:
        return self.where or f"{RAW_BASE} (GitHub)"

    def _raw(self, name: str) -> bytes:
        """The gzipped bytes of one table's CSV."""
        rel = f"csv/{name}.csv.gz"

        if self._zip is not None:
            # A zip made from a folder keeps that folder as a prefix, and
            # which prefix depends on how it was zipped -- so match on the
            # tail rather than guessing.
            for member in self._zip.namelist():
                if member.replace("\\", "/").endswith(rel):
                    return self._zip.read(member)
            raise FileNotFoundError(f"{rel} is not in {self.where}")

        if self.where:
            base = pathlib.Path(self.where)
            path = base / rel
            if not path.exists():
                # Tolerate being pointed at the csv folder itself.
                alt = base / f"{name}.csv.gz"
                if alt.exists():
                    path = alt
                else:
                    raise FileNotFoundError(f"{path} not found")
            return path.read_bytes()

        url = f"{RAW_BASE}/{rel}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"GitHub returned {exc.code} for {rel}. If the repository is "
                f"private this cannot work without a token -- download the "
                f"export and pass --from instead."
            ) from exc

    def rows(self, name: str) -> tuple[list[str], list[list[str]]]:
        data = gzip.decompress(self._raw(name)).decode("utf-8")
        reader = csv.reader(io.StringIO(data))
        header = next(reader, None)
        if not header:
            return [], []
        return header, list(reader)

    def manifest(self) -> dict:
        try:
            if self._zip is not None:
                for m in self._zip.namelist():
                    if m.replace("\\", "/").endswith("manifest.json"):
                        return json.loads(self._zip.read(m))
            elif self.where:
                p = pathlib.Path(self.where) / "manifest.json"
                if p.exists():
                    return json.loads(p.read_text())
            else:
                with urllib.request.urlopen(f"{RAW_BASE}/manifest.json",
                                            timeout=30) as r:
                    return json.loads(r.read())
        except Exception:
            pass
        return {}


def blank_to_none(row: list[str], header: list[str], wanted: tuple[str, ...]):
    """
    Pick the wanted columns out of a CSV row, as values Postgres accepts.

    An empty CSV field means NULL here, not an empty string: every numeric
    and timestamp column in this schema is nullable and would reject "".
    """
    index = {name: i for i, name in enumerate(header)}
    out = []
    for col in wanted:
        i = index.get(col)
        value = row[i] if i is not None and i < len(row) else ""
        out.append(None if value == "" else value)
    return out


# ------------------------------------------------------------------ writing
def sync_dimensions(cur, src: Source) -> dict[str, dict[str, int]]:
    """
    Upsert the dimensions, and return {dimension: {file id -> local id}}.

    That map is the whole point of this function. It is built from the
    natural key, so it stays correct no matter how the two databases
    numbered their rows.
    """
    maps: dict[str, dict[str, int]] = {}

    for table, (natural, columns) in DIMENSIONS.items():
        header, rows = src.rows(table)
        if not rows:
            maps[table] = {}
            continue

        pk = f"{table[4:]}_id" if table.startswith("dim_") else "id"
        pk = {"dim_source": "source_id", "dim_interval": "interval_id",
              "dim_indicator": "indicator_id", "dim_symbol": "symbol_id"}[table]

        # COALESCE on update: a symbol is first written when a quote
        # arrives, carrying no profile at all -- the company name and
        # sector turn up on a later run. Overwriting a real name with the
        # NULL from an older export would undo that, and every Power BI
        # slicer built on it would read "Uncategorized".
        sets = ", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})"
            for c in columns if c != natural
        )
        cols = ", ".join(columns)
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {table} ({cols}) VALUES %s "
            f"ON CONFLICT ({natural}) DO UPDATE SET {sets}",
            [blank_to_none(r, header, columns) for r in rows],
        )

        cur.execute(f"SELECT {natural}, {pk} FROM {table}")
        local = {str(k): v for k, v in cur.fetchall()}

        nat_i = header.index(natural)
        pk_i = header.index(pk)
        maps[table] = {
            r[pk_i]: local[r[nat_i]]
            for r in rows if r[nat_i] in local
        }

    return maps


def load_fact(cur, src: Source, spec, maps) -> tuple[int, int]:
    """Returns (rows in the file, rows actually inserted)."""
    table, columns, fks, conflict = spec
    header, rows = src.rows(table)
    if not rows:
        return 0, 0

    payload = []
    for row in rows:
        values = blank_to_none(row, header, columns)
        broken = False
        for col, dim in fks.items():
            i = columns.index(col)
            raw = values[i]
            if raw is None:
                continue              # symbol_id is nullable on log_api_call
            mapped = maps[dim].get(str(raw))
            if mapped is None:
                broken = True         # a fact whose dimension row is missing
                break
            values[i] = mapped
        if not broken:
            payload.append(values)

    if not payload:
        return len(rows), 0

    cols = ", ".join(columns)
    before = count(cur, table)

    if conflict:
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {table} ({cols}) VALUES %s "
            f"ON CONFLICT ({', '.join(conflict)}) DO NOTHING",
            payload,
        )
    else:
        # log_api_call. No unique constraint to conflict on, and the export
        # is a full snapshot, so without this every import would add the
        # whole log again -- and the dashboard's success rate and the Power
        # BI api view are both computed from it.
        #
        # Matched on when the call happened, to whom, and to which
        # endpoint. Two genuine retries differ in the microseconds of
        # called_at_utc, so real duplicates survive and copies do not.
        cur.execute(f"""
            CREATE TEMP TABLE _incoming (LIKE {table} INCLUDING DEFAULTS)
            ON COMMIT DROP
        """)
        psycopg2.extras.execute_values(
            cur, f"INSERT INTO _incoming ({cols}) VALUES %s", payload)
        cur.execute(f"""
            INSERT INTO {table} ({cols})
            SELECT {cols} FROM _incoming i
             WHERE NOT EXISTS (
                   SELECT 1 FROM {table} t
                    WHERE t.called_at_utc = i.called_at_utc
                      AND t.endpoint      = i.endpoint
                      AND t.source_id     = i.source_id)
        """)
        cur.execute("DROP TABLE _incoming")

    return len(rows), count(cur, table) - before


def count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def fix_sequences(cur) -> None:
    """
    Move every sequence past the ids now present.

    These inserts let the sequence assign ids, so this is belt and braces
    -- but it costs nothing and it matters if you ever also load a CSV by
    hand, where \\copy writes the id column explicitly and leaves the
    counter behind. The next insert then collides with an existing id,
    which reads as a corrupt database and is only a stale counter.
    """
    cur.execute("""
        SELECT format(
            'SELECT setval(%L, COALESCE((SELECT MAX(%I) FROM %I), 1), '
            'EXISTS (SELECT 1 FROM %I))',
            pg_get_serial_sequence(table_name, column_name),
            column_name, table_name, table_name)
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND pg_get_serial_sequence(table_name, column_name) IS NOT NULL
    """)
    for (statement,) in cur.fetchall():
        cur.execute(statement)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Load the published export into your own PostgreSQL.")
    ap.add_argument("--target", default=os.getenv("LOCAL_DATABASE_URL", ""),
                    help="your database (default: $LOCAL_DATABASE_URL)")
    ap.add_argument("--from", dest="source", default=None,
                    help="a folder or .zip; omit to fetch from GitHub")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be loaded, write nothing")
    args = ap.parse_args(argv)

    if not args.target:
        print("\n  --target is required: the database the data goes INTO.\n"
              "    --target postgresql://postgres:PASSWORD@localhost:5432/marketdb\n"
              "  Or set LOCAL_DATABASE_URL.\n", file=sys.stderr)
        return 2

    now = datetime.now(ZoneInfo(APP_TIMEZONE))
    print(f"\n  {now:%Y-%m-%d %H:%M:%S} {APP_TIMEZONE}")

    try:
        src = Source(args.source)
    except Exception as exc:
        print(f"\n  Cannot open the export: {exc}\n", file=sys.stderr)
        return 1
    print(f"  from   {src.label()}")

    info = src.manifest()
    if info.get("exported_at"):
        print(f"  built  {info['exported_at'].get('local', '?')} Berlin")

    try:
        conn = psycopg2.connect(args.target, connect_timeout=15)
    except psycopg2.Error as exc:
        print(f"\n  Cannot reach your database.\n    {str(exc).strip()}\n",
              file=sys.stderr)
        return 1

    host = psycopg2.extensions.parse_dsn(args.target)
    print(f"  into   {host.get('host', 'localhost')}/{host.get('dbname', '?')}\n")

    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.dim_symbol')")
            if cur.fetchone()[0] is None:
                print("  The tables are not there yet. Create them first:\n"
                      "    python tools/local_setup.py --target \"<same url>\" "
                      "--schema-only\n", file=sys.stderr)
                return 1

            maps = sync_dimensions(cur, src)
            for table, (natural, _cols) in DIMENSIONS.items():
                print(f"  {table:<26} {len(maps[table]):>6} mapped")

            total_new = 0
            for spec in FACTS:
                read, new = load_fact(cur, src, spec, maps)
                total_new += new
                flag = "" if new else "   (nothing new)"
                print(f"  {spec[0]:<26} {read:>6} in file  "
                      f"{new:>6} added{flag}")

            fix_sequences(cur)

            if args.dry_run:
                conn.rollback()
                print(f"\n  Dry run: {total_new} row(s) would have been added. "
                      f"Nothing was written.\n")
                return 0

        print(f"\n  {total_new} row(s) added.\n")
    except psycopg2.Error as exc:
        print(f"\n  The import failed and nothing was written:\n"
              f"    {str(exc).strip()}\n", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
