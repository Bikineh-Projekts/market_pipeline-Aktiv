#!/usr/bin/env python3
"""
Fail if a pinned dependency has no prebuilt wheel for the pinned Python.

This exists because of a real deploy failure. Render defaults new services
to Python 3.14, and psycopg2-binary published no cp314 wheel until 2.9.13.
pip therefore compiled 2.9.9 from source, produced a .so referencing
_PyInterpreterState_Get -- a symbol 3.14 no longer exports -- and the
service crashed on `import psycopg2`, several minutes into the deploy,
with a build log that said "Successfully installed".

A source build is the warning sign. It usually succeeds and then fails at
import, which is the worst possible order.

    python tools/check_wheels.py

Exit 0 when every pinned package has a matching wheel, 1 otherwise.
Needs network access; skips with a notice if PyPI is unreachable, so an
outage does not block a build.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent

# Pure-Python packages ship a single py3-none-any wheel and need no check
# against the interpreter tag.
PIN = re.compile(r"^([A-Za-z0-9._-]+)==([A-Za-z0-9.]+)\s*$")


def pinned_python() -> str:
    path = REPO / ".python-version"
    if not path.exists():
        print("::error::.python-version is missing, so Render picks the newest "
              "Python and this check has nothing to compare against")
        sys.exit(1)
    return path.read_text().strip()


def requirement_files() -> list[pathlib.Path]:
    return [p for p in (REPO / "requirements.txt",
                        REPO / "requirements-web.txt",
                        REPO / "requirements-dev.txt") if p.exists()]


def pins() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in requirement_files():
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            match = PIN.match(line)
            if match:
                found[match.group(1)] = match.group(2)
    return found


def wheel_tags(package: str, version: str) -> set[str] | None:
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"::notice::could not reach PyPI for {package}=={version}: {exc}")
        return None

    tags: set[str] = set()
    for item in data.get("urls", []):
        name = item.get("filename", "")
        if name.endswith(".whl"):
            parts = name.split("-")
            if len(parts) >= 3:
                tags.add(parts[2])
    return tags


def main() -> int:
    version = pinned_python()
    major_minor = ".".join(version.split(".")[:2])
    tag = "cp" + major_minor.replace(".", "")

    print(f"Pinned Python: {version}  (wheel tag {tag})")

    problems: list[str] = []
    unreachable = 0

    for package, pin in sorted(pins().items()):
        tags = wheel_tags(package, pin)
        if tags is None:
            unreachable += 1
            continue

        if not tags:
            problems.append(f"{package}=={pin} publishes no wheels at all; "
                            f"pip will build it from source")
            continue

        # py3-none-any covers every interpreter.
        if any(t.startswith("py3") or t == "py2.py3" for t in tags):
            print(f"  {package}=={pin}: pure-Python wheel")
            continue

        if tag in tags:
            print(f"  {package}=={pin}: {tag} wheel present")
        else:
            available = ", ".join(sorted(t for t in tags if t.startswith("cp"))) or "none"
            problems.append(
                f"{package}=={pin} has no {tag} wheel (has: {available}). "
                f"pip would compile it from source, which usually succeeds and "
                f"then fails at import. Bump the package, or change "
                f".python-version to a release it supports.")

    for line in problems:
        print(f"::error::{line}")

    if unreachable:
        print(f"::notice::{unreachable} package(s) could not be checked "
              f"because PyPI was unreachable")

    if problems:
        print(f"\n{len(problems)} dependency pin(s) would be built from source.")
        return 1

    print("\nEvery pinned dependency has a wheel for the pinned Python.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
