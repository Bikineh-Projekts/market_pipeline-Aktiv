"""
Every tool must work when RUN, not only when imported by a test.

This file exists because the whole suite was green while every scheduled
export failed, for a day and a half, on a one-line import.

The workflows run these tools as scripts:

    python tools/export_data.py --url "$DATABASE_URL" --out export

Python then puts tools/ on sys.path -- NOT the repository root. So
`from tools import pgconnect` raises ModuleNotFoundError. pytest imports
the same file as `tools.export_data`, with the root on the path, where
that import resolves perfectly. The test and the workflow were running
the same code under two different import roots, and only the test's root
was ever checked.

So the gap was never in the code. It was in the shape of the test: the
suite proved the module imports the way the suite imports it. These tests
run each tool in a subprocess, exactly as its workflow does, so an import
that only works under pytest fails here.

--help is enough to catch it, because every module-level import runs
before argparse does. That is also why the pgconnect import in
export_data was moved to module level: inside a function it would only
have failed mid-export, which is how this one stayed hidden.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Exactly the invocations in .github/workflows/*.yml, plus the tools a
# person is told to run by hand. Keep this list in step with the
# workflows -- test_every_tool_a_workflow_runs_is_listed_here does that.
SCRIPTS = [
    "tools/export_data.py",
    "tools/check_export_safe.py",
    "tools/check_wheels.py",
    "tools/check_dockerfile.py",
    "tools/check_python_version.py",
    "tools/check_no_credentials.py",
    "tools/annotate_pytest.py",
    "tools/import_export.py",
    "tools/local_setup.py",
    "tools/replicate.py",
    "tools/set_secrets.py",
    "tools/healthcheck.py",
]


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO,            # the workflows' working directory
        capture_output=True,
        text=True,
        timeout=120,
        input="",            # never block on a prompt
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_tool_imports_when_run_as_a_script(script):
    """
    `python tools/X.py --help` must not die on an import.

    A tool without argparse exits non-zero on --help, which is fine --
    what is not fine is ModuleNotFoundError or ImportError, because those
    happen before the tool can do anything at all.
    """
    done = run([script, "--help"])
    output = done.stdout + done.stderr
    for fatal in ("ModuleNotFoundError", "ImportError", "SyntaxError",
                  "IndentationError", "NameError"):
        assert fatal not in output, (
            f"{script} cannot be run as a script:\n"
            f"{output.strip()[-800:]}\n\n"
            f"Run as a script, sys.path[0] is tools/, not the repository "
            f"root. Put the root on sys.path at module level, the way "
            f"replicate.py and import_export.py do."
        )


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_tool_does_not_import_the_tools_package_lazily(script):
    """
    A deferred `from tools import ...` hides this failure until runtime.

    export_data had one inside _host(), so --help passed, the tests
    passed, and the export died three quarters of the way through a real
    run against a real database. An import that can fail should fail at
    import time, where every check can see it.
    """
    source = (REPO / script).read_text()
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), 1)
        if line.startswith((" ", "\t"))
        and ("from tools import" in line.split("#", 1)[0]
             or line.split("#", 1)[0].strip().startswith("import tools"))
        # local_setup imports replicate inside main() deliberately, after
        # its connection checks, and says so on the line itself.
        and "noqa: PLC0415" not in line
    ]
    assert not offenders, (
        f"{script} imports from tools/ inside a function:\n  "
        + "\n  ".join(offenders)
        + "\n\nMove it to module level so it is checked at import time."
    )


def test_every_tool_a_workflow_runs_is_listed_here():
    """
    A new `python tools/...` line in a workflow joins this file, or the
    next one of these is found the same way: by its absence in production.
    """
    import re

    invoked = set()
    for workflow in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        for match in re.finditer(r"python3?\s+(tools/[\w_]+\.py)", workflow.read_text()):
            invoked.add(match.group(1))

    missing = sorted(invoked - set(SCRIPTS))
    assert not missing, (
        "these are run by a workflow but not tested as scripts: "
        + ", ".join(missing)
    )


def test_export_data_can_label_a_host_when_run_as_a_script():
    """
    The exact call that failed, in the exact import context that failed.

    _host's return value goes into manifest.json, which is force-pushed to
    a public branch, so this one is worth pinning rather than trusting the
    general check above.
    """
    done = run([
        "-c",
        "import runpy;"
        "m = runpy.run_path('tools/export_data.py');"
        "print(m['_host']('postgresql://u:SECRET@h.example.com/db'))",
    ])
    assert done.returncode == 0, done.stderr[-600:]
    assert done.stdout.strip() == "h.example.com/db"
    assert "SECRET" not in done.stdout


def test_healthcheck_is_listed_as_a_script():
    """It is run by a person, not a workflow, so nothing else would check it."""
    assert "tools/healthcheck.py" in SCRIPTS


def test_the_healthcheck_sql_exists_and_writes_nothing():
    """
    It is handed to someone to run against a database holding real data.
    A SELECT-only script cannot damage anything; one stray UPDATE could.
    The single CREATE TABLE IF NOT EXISTS is deliberate and explained in
    the file -- replication_state is made by replicate.py, not the
    schema, and SQL resolves every table name before running anything.
    """
    sql = (REPO / "sql" / "07_healthcheck.sql").read_text()
    body = "\n".join(
        line.split("--", 1)[0] for line in sql.splitlines()
    ).upper()
    for forbidden in ("UPDATE ", "DELETE ", "INSERT ", "DROP ", "ALTER ",
                      "TRUNCATE", "GRANT ", "REVOKE "):
        assert forbidden not in body, f"{forbidden.strip()} in a read-only check"
    assert body.count("CREATE ") == 1
    assert "CREATE TABLE IF NOT EXISTS REPLICATION_STATE" in body
