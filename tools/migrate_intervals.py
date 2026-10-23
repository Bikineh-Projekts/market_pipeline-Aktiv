#!/usr/bin/env python3
"""
Apply sql/08_merge_duplicate_intervals.sql to both databases.

    tools\\migrate_intervals.cmd

Reads TARGET (your PostgreSQL) and SOURCE (Render) from tools/secrets.cmd
and runs the migration against each, printing what it found and what it
changed. Safe to run again: on a database that is already clean it
reports nothing to do and changes nothing.

The migration is one transaction per database. If anything raises, that
database is rolled back whole and the other is still attempted -- one of
them failing is not a reason to leave the other broken.

A note on the SQL file: it is written to be pasted into psql as well, so
it carries \\echo lines. psycopg2 does not speak those -- they are a psql
client feature, not SQL -- so they are stripped here. The before/after
reports are run from Python instead, which also means they print as real
tables rather than as whatever the last statement happened to return.
"""

from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import pgconnect  # noqa: E402

MIGRATION = REPO / "sql" / "08_merge_duplicate_intervals.sql"
SECRETS = HERE / "secrets.cmd"

REPORT = """
SELECT i.interval_code,
       (SELECT COUNT(*) FROM fact_market_timeseries f WHERE f.interval_id = i.interval_id) AS candles,
       (SELECT COUNT(*) FROM fact_market_indicator  f WHERE f.interval_id = i.interval_id) AS indicators
  FROM dim_interval i ORDER BY i.interval_code
"""


def urls_from_secrets() -> list[tuple[str, str]]:
    if not SECRETS.exists():
        raise SystemExit(f"\n  {SECRETS} does not exist.\n"
                         f"  Run tools\\set_secrets.cmd first.\n")

    found = []
    for line in SECRETS.read_text(errors="replace").splitlines():
        match = re.match(r'\s*set\s+"?(TARGET|SOURCE)=(.*?)"?\s*$', line, re.IGNORECASE)
        if match:
            name, url = match.group(1).upper(), match.group(2).replace("%%", "%")
            found.append(("your own database" if name == "TARGET" else "Render", url))

    if not found:
        raise SystemExit(f"\n  No TARGET or SOURCE line in {SECRETS}.\n")
    return found


def show(cur, heading: str) -> None:
    cur.execute(REPORT)
    rows = cur.fetchall()
    print(f"    {heading}")
    for code, candles, indicators in rows:
        print(f"      {code:<10} {candles:>7} candles  {indicators:>6} indicators")


def migrate(label: str, url: str, sql: str) -> bool:
    print(f"\n  {label}: {pgconnect.safe_label(url)}")
    try:
        conn = pgconnect.connect(url, timeout=30)
    except pgconnect.ConnectionFailed as exc:
        print(f"    CANNOT CONNECT  {str(exc).splitlines()[0]}")
        return False

    try:
        with conn.cursor() as cur:
            show(cur, "before:")

            cur.execute("""
                SELECT interval_code FROM dim_interval
                 WHERE interval_code IN ('daily','weekly','monthly')
                 ORDER BY interval_code
            """)
            aliases = [r[0] for r in cur.fetchall()]
            if not aliases:
                print("    nothing to do -- already one code per interval.")
                conn.rollback()
                return True

            print(f"    merging: {', '.join(aliases)}")
            cur.execute(sql)
            show(cur, "after:")
        conn.commit()
        print("    committed.")
        return True
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"    FAILED, rolled back: {str(exc).strip().splitlines()[0]}")
        return False
    finally:
        conn.close()


def main() -> int:
    if not MIGRATION.exists():
        raise SystemExit(f"\n  {MIGRATION} is missing.\n")

    # \echo is psql's, not SQL's. BEGIN/COMMIT go too: psycopg2 opens the
    # transaction itself, and a COMMIT inside the script would end it
    # early, leaving the rest of the migration outside the all-or-nothing
    # guarantee that is the whole point of running it in one.
    sql = "\n".join(
        line for line in MIGRATION.read_text().splitlines()
        if not line.strip().startswith("\\echo")
        and line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )

    print("\n  Merging duplicate interval codes.")
    ok = [migrate(label, url, sql) for label, url in urls_from_secrets()]

    print()
    if all(ok):
        print("  Done. Check it with: tools\\healthcheck.cmd\n")
        return 0
    print("  At least one database was not migrated -- see above.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
