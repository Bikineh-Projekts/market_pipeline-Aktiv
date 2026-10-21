#!/usr/bin/env python3
"""
Run sql/07_healthcheck.sql against your own database and print the result.

    tools\\healthcheck.cmd          (or: python tools/healthcheck.py)

It reads the connection string from tools/secrets.cmd, the same file the
scheduled pull uses, so there is nothing to type and no password on a
command line -- a password on a command line is visible to every other
process on the machine and lands in the console history.

It writes nothing and holds no locks, so it is safe to run while a pull
is in progress.

Exit code 0 if every check passed, 1 if anything says WARN, 2 if anything
says FAIL -- so it can be put in a scheduled task later and be noticed.
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

SQL_FILE = REPO / "sql" / "07_healthcheck.sql"
SECRETS = HERE / "secrets.cmd"


def target_from_secrets() -> str:
    """
    The TARGET line out of secrets.cmd.

    A '%' is stored doubled in a .cmd file, because cmd eats them in
    pairs, so it has to be halved on the way back out or the password is
    wrong in a way that reads as "authentication failed".
    """
    if not SECRETS.exists():
        raise SystemExit(
            f"\n  {SECRETS} does not exist.\n"
            f"  Run tools\\set_secrets.cmd first.\n"
        )

    for line in SECRETS.read_text(errors="replace").splitlines():
        match = re.match(r'\s*set\s+"?TARGET=(.*?)"?\s*$', line, re.IGNORECASE)
        if match:
            return match.group(1).replace("%%", "%")

    raise SystemExit(
        f"\n  No TARGET line in {SECRETS}.\n"
        f"  Run tools\\set_secrets.cmd to rewrite it.\n"
    )


def main() -> int:
    if not SQL_FILE.exists():
        raise SystemExit(
            f"\n  {SQL_FILE} is missing. Download it next to the others:\n"
            f"    curl -fsSL -o sql\\07_healthcheck.sql "
            f"https://raw.githubusercontent.com/Bikineh-Projekts/"
            f"market_pipeline-Aktiv/main/sql/07_healthcheck.sql\n"
        )

    url = target_from_secrets()
    wrong = pgconnect.complain_about_url(url)
    if wrong:
        raise SystemExit(f"\n  The TARGET in secrets.cmd is not usable: {wrong}\n")

    print(f"\n  checking {pgconnect.safe_label(url)}\n")

    try:
        conn = pgconnect.connect(url, autocommit=True)
    except pgconnect.ConnectionFailed as exc:
        raise SystemExit(f"\n  Cannot reach your database.\n    {exc}\n") from None

    with conn, conn.cursor() as cur:
        cur.execute(SQL_FILE.read_text())
        rows = cur.fetchall()
    conn.close()

    # Widths from the data, so nothing is cut off and nothing is padded
    # into the next screen.
    checks = [(str(c), str(f), str(s), str(n or "")) for _, c, f, s, n in rows]
    w_check = max(len(c) for c, _, _, _ in checks)
    w_found = max(len(f) for _, f, _, _ in checks)

    for check, found, status, note in checks:
        mark = {"OK": "  ok ", "WARN": " WARN", "FAIL": " FAIL"}.get(status, status)
        line = f"  {mark}  {check:<{w_check}}  {found:<{w_found}}"
        if status != "OK" and note:
            line += f"   <- {note}"
        print(line)

    fails = sum(1 for _, _, s, _ in checks if s == "FAIL")
    warns = sum(1 for _, _, s, _ in checks if s == "WARN")

    print()
    if fails:
        print(f"  {fails} FAILED, {warns} warning(s). The FAIL lines are the ones to act on.\n")
        return 2
    if warns:
        print(f"  No failures, {warns} warning(s) -- usually explainable, worth a look.\n")
        return 1
    print("  Everything passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
