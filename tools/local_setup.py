#!/usr/bin/env python3
"""
Set up the local copy in one command.

WHY THIS EXISTS
---------------
Nothing in this project can push data into your PostgreSQL. Your database
listens on your own machine, behind your own router; GitHub Actions and
Render are on the public internet and have no route to it, no credentials
for it, and no idea it exists. Data arrives in your Postgres only when
something ON your machine goes and fetches it. That something is
tools/replicate.py, and this script is what gets you to the point where
running it works.

It is safe to run again. Every step checks before it acts, and the schema
files are written to be re-applied.

    python tools/local_setup.py \
        --source "postgresql://...render.com/market_db_aktiv" \
        --target "postgresql://postgres:PASSWORD@localhost:5432/marketdb"

Leave --target off and it uses $LOCAL_DATABASE_URL; leave --source off and
it uses $SOURCE_DATABASE_URL.

WHAT IT DOES, IN ORDER
    1. connects to your database, and says plainly what went wrong if it
       cannot -- a wrong password and a database that does not exist look
       nothing alike, and neither looks like "no file arrived"
    2. applies sql/01_schema.sql and sql/02_bi_views.sql to it
    3. copies everything across, once
    4. prints the one command that keeps it current

psql is NOT required. Everything here goes through psycopg2, because
pgAdmin installs do not necessarily put psql on PATH, and "psql is not
recognized as an internal or external command" is a poor welcome.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime

import psycopg2

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import pgconnect  # noqa: E402

SCHEMA_FILES = ("sql/01_schema.sql", "sql/02_bi_views.sql")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Berlin")


def say(step: str, detail: str = "") -> None:
    print(f"  {step:<28} {detail}".rstrip(), flush=True)


def fail(headline: str, *lines: str) -> int:
    print(f"\n  FAILED: {headline}", file=sys.stderr)
    for line in lines:
        print(f"    {line}", file=sys.stderr)
    return 1


def describe(url: str) -> str:
    """host/database, never the password. See pgconnect.safe_label."""
    return pgconnect.safe_label(url)


def explain_connection_error(exc: Exception, url: str, label: str) -> list[str]:
    """
    Turn libpq's message into the thing to actually go and do.

    These four cover essentially every first-run failure, and each one has
    a different fix -- which is the whole point of separating them.

    Matched in German as well as English. A German PostgreSQL says
    "Datenbank »x« existiert nicht", and matching only the English would
    drop every one of these cases through to the generic branch on exactly
    the machine that needs them most.
    """
    text = str(exc).lower()
    where = describe(url)

    if ("does not exist" in text and "database" in text) \
            or ("existiert nicht" in text and "datenbank" in text):
        # Through safe_label, not parse_dsn: on a connection string nested
        # inside itself parse_dsn returns the whole remainder -- password
        # and all -- as the dbname, and this prints it twice.
        name = pgconnect.safe_label(url).rsplit("/", 1)[-1] or "marketdb"
        return [
            f'the {label} database "{name}" does not exist yet.',
            "Create it first -- in pgAdmin: right-click Databases -> Create",
            f'-> Database, name it "{name}". Then run this again.',
        ]
    if ("password authentication failed" in text
            or "no password supplied" in text
            or "passwort-authentifizierung" in text
            or "kein passwort" in text):
        return [
            f"{where} refused the password.",
            "The password belongs in the connection string, between the user",
            "name and the @:  postgresql://postgres:PASSWORD@localhost:5432/marketdb",
            "If it contains @ : / or #, percent-encode those characters.",
        ]
    if ("could not connect" in text or "connection refused" in text
            or "timeout" in text or "verbindung" in text):
        return [
            f"nothing is listening at {where}.",
            "For a local database: check the PostgreSQL service is running",
            "  (Windows: services.msc -> postgresql-x64-16 -> Start)",
            "and that the port is right -- 5432 unless you changed it.",
            "For Render: use the EXTERNAL database URL, not the internal one.",
            "The internal hostname resolves only inside Render.",
        ]
    return [f"{where}: {exc}"]


def connect(url: str, label: str):
    # Through pgconnect, so a German or French PostgreSQL reports a bad
    # password as a bad password rather than as a UnicodeDecodeError.
    return pgconnect.connect(url, autocommit=True)


def apply_schema(conn) -> None:
    for name in SCHEMA_FILES:
        path = REPO / name
        if not path.exists():
            raise FileNotFoundError(
                f"{name} is missing. Run this from inside the repository, "
                f"not from a copy of the tools folder on its own."
            )
        with conn.cursor() as cur:
            cur.execute(path.read_text())
        say(name, "applied")


def count_rows(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
             ORDER BY table_name
        """)
        tables = [r[0] for r in cur.fetchall()]
        out = {}
        for t in tables:
            cur.execute(f'SELECT COUNT(*) FROM "{t}"')
            out[t] = cur.fetchone()[0]
        return out



def display_zone() -> tuple[object, str]:
    """
    The zone to print times in, and an honest label for it.

    Windows ships no IANA time-zone database, so ZoneInfo("Europe/Berlin")
    raises ZoneInfoNotFoundError there unless the `tzdata` package is
    installed -- and it crashed this tool on the user's machine before it
    had connected to anything, over a line that only prints a header.

    Falling back to UTC, which the older tools here do, is worse than it
    looks: they go on printing the label "Europe/Berlin" above times that
    are two hours out. The system's own local zone is the better guess --
    on the machine this runs on it IS Berlin -- and the label says which
    one was actually used, so a wrong clock cannot pass for the right one.
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(APP_TIMEZONE), APP_TIMEZONE
    except Exception:
        local = datetime.now().astimezone().tzinfo
        name = datetime.now().astimezone().tzname() or "local time"
        return local, f"{name} (system clock; pip install tzdata for {APP_TIMEZONE})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Prepare your own PostgreSQL and fill it, once.",
    )
    ap.add_argument("--source", default=os.getenv("SOURCE_DATABASE_URL", ""),
                    help="the Render External Database URL "
                         "(default: $SOURCE_DATABASE_URL)")
    ap.add_argument("--target", default=os.getenv("LOCAL_DATABASE_URL", ""),
                    help="your own PostgreSQL (default: $LOCAL_DATABASE_URL)")
    ap.add_argument("--schema-only", action="store_true",
                    help="create the tables and stop, copying nothing")
    args = ap.parse_args(argv)

    if not args.target:
        return fail(
            "no --target given.",
            "This is YOUR database, the one the data should end up in:",
            "  --target postgresql://postgres:PASSWORD@localhost:5432/marketdb",
            "Or set LOCAL_DATABASE_URL.",
        )
    if not args.schema_only and not args.source:
        return fail(
            "no --source given.",
            "This is the Render database to copy FROM. In the Render",
            "dashboard, open the database and copy the External Database URL.",
            "Or set SOURCE_DATABASE_URL.",
            "",
            "To only create the empty tables for now, pass --schema-only.",
        )
    if args.source and args.source == args.target:
        return fail("--source and --target are the same database.")

    zone, zone_label = display_zone()
    now = datetime.now(zone)
    print(f"\n  {now:%Y-%m-%d %H:%M:%S} {zone_label}\n")

    say("your database", describe(args.target))
    try:
        target = connect(args.target, "target")
    except Exception as exc:
        return fail("cannot reach your database.",
                    *explain_connection_error(exc, args.target, "target"))

    with target.cursor() as cur:
        cur.execute("SELECT current_user, version()")
        user, version = cur.fetchone()
    say("", f"connected as {user}, {version.split(',')[0]}")

    print()
    say("creating the tables", "")
    try:
        apply_schema(target)
    except psycopg2.Error as exc:
        return fail("the schema would not apply.", str(exc).strip())

    before = count_rows(target)
    say("tables present", f"{len(before)}")

    if args.schema_only:
        print("\n  Tables are ready. Nothing was copied (--schema-only).")
        print("  Run again with --source to fill them.\n")
        return 0

    print()
    say("copying from", describe(args.source))
    target.close()

    from tools import replicate  # noqa: PLC0415  -- after the checks above

    # Deliberately not --full. On an empty target the ordinary pass already
    # copies everything, and on a re-run --full is the wrong thing to do:
    # it re-reads from zero, which log_api_call has no unique key to absorb.
    rc = replicate.main([
        "--source", args.source,
        "--target", args.target,
        "--once",
    ])
    if rc != 0:
        return fail("the first copy did not finish.",
                    "Everything above it worked, so this is about reaching",
                    "Render: check the External URL and that the database",
                    "has not expired (free databases last 30 days).")

    target = connect(args.target, "target")
    after = count_rows(target)
    total = sum(after.values())
    print()
    for name in sorted(after):
        if after[name]:
            say(name, f"{after[name]:>7} row(s)")
    say("TOTAL", f"{total:>7} row(s)")

    print(f"""
  Done. Your database now holds a full copy.

  To keep it current, leave this running -- it copies only what is new,
  so a pass with nothing to do costs a few milliseconds:

      python tools/replicate.py --loop 10 \\
          --source "<the same source URL>" \\
          --target "<the same target URL>" \\
          --keep-awake https://market-pipeline-aktiv.onrender.com

  On Windows, tools/local_sync.cmd does exactly that and restarts itself
  if it stops. See docs/LOCAL_COPY.md.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
