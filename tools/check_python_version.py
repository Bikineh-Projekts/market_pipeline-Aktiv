#!/usr/bin/env python3
"""
One Python version, declared once.

This project ran its tests on one version and its ETL on another for
weeks. CI read .python-version (3.12); etl.yml and database.yml -- the two
workflows that actually write to the production database -- said "3.11".
Nothing broke, but nothing was being tested either: the same shape of gap
that let a migration pass CI and fail against the live database.

It is not a theoretical worry for this project specifically. A deploy
already failed once because Render picked a Python with no psycopg2-binary
wheel for it, so pip built the extension from source and produced a .so
that would not import. Which interpreter runs the code is load-bearing
here, and a number repeated in five files drifts.

So: .python-version is the single source, the Dockerfile must agree with
it, and no workflow may pin a version of its own.

    python tools/check_python_version.py
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github/workflows"


def fail(message: str) -> None:
    print(f"::error title=Python version::{message}")
    print(f"  {message}", file=sys.stderr)


def main() -> int:
    problems = 0

    version_file = REPO / ".python-version"
    if not version_file.exists():
        fail(".python-version is missing. It is what CI, the Dockerfile and "
             "Render all read.")
        return 1
    wanted = version_file.read_text().strip()
    if not re.fullmatch(r"\d+\.\d+", wanted):
        fail(f".python-version says {wanted!r}; expected something like 3.12.")
        problems += 1

    # The Dockerfile is what Render actually runs, so it has to agree.
    dockerfile = REPO / "Dockerfile"
    if dockerfile.exists():
        found = re.search(r"^FROM\s+python:(\d+\.\d+)", dockerfile.read_text(),
                          re.M)
        if not found:
            fail("Dockerfile does not start FROM a pinned python:X.Y image.")
            problems += 1
        elif found.group(1) != wanted:
            fail(f"Dockerfile uses python:{found.group(1)} but "
                 f".python-version says {wanted}. Render would run one "
                 f"version while everything else tests another.")
            problems += 1

    # No workflow may carry its own number.
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r'python-version:\s*["\']?\d+\.\d+', line):
                fail(f"{path.name}:{n} pins a Python version. Use "
                     f"`python-version-file: .python-version` so there is "
                     f"one number to change, not five.")
                problems += 1

    if problems:
        print(f"\n  {problems} problem(s). One version, declared once.",
              file=sys.stderr)
        return 1

    print(f"  Python {wanted} everywhere: .python-version, the Dockerfile, "
          f"and every workflow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
