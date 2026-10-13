#!/usr/bin/env python3
"""
Turn a pytest run's output into GitHub Actions annotations.

Why: a failing CI job's reason lives in the raw log, which takes several
clicks to reach and is not always retrievable through the API at all.
Annotations appear on the run summary and in the checks API, so "which
test failed and on what assertion" is visible without downloading
anything.

    python -m pytest -v --tb=short 2>&1 | tee pytest.log
    python tools/annotate_pytest.py pytest.log

Exits 1 if the log shows failures or errors, 0 otherwise, so it can carry
the step's verdict.
"""

from __future__ import annotations

import pathlib
import re
import sys

# "FAILED tests/test_web.py::test_name - AssertionError: msg"
RESULT = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$")
# The summary line: "=== 3 failed, 107 passed in 45.12s ==="
SUMMARY = re.compile(r"^=+\s*(.*?(?:passed|failed|error).*?)\s*=+$")
# An assertion line inside a short traceback: "tests/x.py:42: AssertionError"
ASSERT_LINE = re.compile(r"^(\S+\.py):(\d+):\s*(\w*Error.*)$")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("::error::annotate_pytest.py needs a log file path")
        return 1

    path = pathlib.Path(argv[1])
    if not path.exists():
        print(f"::error::pytest log not found at {path}")
        return 1

    lines = path.read_text(errors="replace").splitlines()

    failures: list[tuple[str, str, str]] = []   # (kind, test id, reason)
    locations: dict[str, tuple[str, str]] = {}  # test id -> (file, line)
    summary = ""
    current = ""

    for line in lines:
        stripped = line.rstrip()

        match = SUMMARY.match(stripped)
        if match and ("passed" in match.group(1) or "failed" in match.group(1)):
            summary = match.group(1)

        # Section headers in a verbose traceback name the test.
        header = re.match(r"^_{3,}\s+(\S+)\s+_{3,}$", stripped)
        if header:
            current = header.group(1)
            continue

        loc = ASSERT_LINE.match(stripped)
        if loc and current:
            locations.setdefault(current, (loc.group(1), loc.group(2)))

        match = RESULT.match(stripped)
        if match:
            failures.append((match.group(1), match.group(2), match.group(3) or ""))

    for kind, test_id, reason in failures:
        name = test_id.rsplit("::", 1)[-1]
        where = ""
        for key, (file_name, line_number) in locations.items():
            if key.endswith(name):
                where = f" file={file_name},line={line_number}"
                break
        detail = reason or "see the job log for the traceback"
        print(f"::error{where},title={kind}: {name}::{detail}")

    if summary:
        print(f"::notice title=pytest::{summary}")

    if failures:
        print(f"\n{len(failures)} test(s) failed:")
        for kind, test_id, reason in failures:
            print(f"  {kind}  {test_id}  {reason}")
        return 1

    # No FAILED lines, but the run may still have died before collecting --
    # an import error, a missing database, a pytest.exit().
    if not summary:
        print("::error title=pytest::The run produced no result summary, so it "
              "stopped before finishing. Common causes: the test database was "
              "unreachable, or a module failed to import.")
        tail = [l for l in lines[-25:] if l.strip()]
        for line in tail:
            print(f"::error::{line[:400]}")
        return 1

    if "failed" in summary or "error" in summary:
        print(f"::error title=pytest::{summary}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
