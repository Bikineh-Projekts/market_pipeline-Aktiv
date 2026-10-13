#!/usr/bin/env python3
"""
Static checks on the Dockerfile, for the mistakes that only surface at
deploy time otherwise.

A renamed or moved file breaks `docker build` on Render, several minutes
into a deploy, with a message about a path that no longer exists. Catching
it in CI costs a second.

    python tools/check_dockerfile.py

Exit 0 when clean, 1 on a problem. This does not build the image -- it
checks the things that can be checked without a daemon.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def _instructions(text: str) -> list[tuple[int, str, str]]:
    """(line number, instruction, arguments), with continuations joined."""
    out: list[tuple[int, str, str]] = []
    buffer, start = "", 0

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not buffer and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if not buffer:
            start = number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        parts = buffer.strip().split(maxsplit=1)
        if parts:
            out.append((start, parts[0].upper(), parts[1] if len(parts) > 1 else ""))
        buffer = ""

    return out


def _ignore_patterns() -> tuple[list[str], set[str]]:
    path = REPO / ".dockerignore"
    if not path.exists():
        return [], set()
    entries = [l.strip() for l in path.read_text().splitlines()
               if l.strip() and not l.lstrip().startswith("#")]
    keep = {e[1:].rstrip("/") for e in entries if e.startswith("!")}
    drop = [e.rstrip("/") for e in entries if not e.startswith("!")]
    return drop, keep


def main() -> int:
    dockerfile = REPO / "Dockerfile"
    if not dockerfile.exists():
        print("::error::Dockerfile not found")
        return 1

    text = dockerfile.read_text()
    instructions = _instructions(text)
    drop, keep = _ignore_patterns()
    problems: list[str] = []
    copied: list[str] = []

    for number, name, args in instructions:
        if name not in ("COPY", "ADD"):
            continue
        # Drop flags such as --from=builder or --chown=user:group.
        tokens = [t for t in args.split() if not t.startswith("--")]
        if len(tokens) < 2:
            problems.append(f"line {number}: {name} needs a source and a destination")
            continue
        # A COPY --from=stage refers to an earlier build stage, not the repo.
        if "--from=" in args:
            continue

        for source in tokens[:-1]:
            copied.append(source)
            clean = source.rstrip("/")
            if not (REPO / clean).exists():
                problems.append(
                    f"line {number}: {name} source '{source}' does not exist in the repo")
                continue
            for pattern in drop:
                if fnmatch.fnmatch(clean, pattern) and clean not in keep:
                    problems.append(
                        f"line {number}: {name} copies '{source}' but .dockerignore "
                        f"excludes it via '{pattern}', so the build context will not have it")

    names = [name for _, name, _ in instructions]
    if "FROM" not in names:
        problems.append("no FROM instruction")
    if not any(n in ("CMD", "ENTRYPOINT") for n in names):
        problems.append("no CMD or ENTRYPOINT, so the container has nothing to run")

    # Shell-form CMD means the process is a child of /bin/sh, which does not
    # forward SIGTERM. Render's shutdown then times out and force-kills.
    for number, name, args in instructions:
        if name == "CMD" and not args.strip().startswith("["):
            problems.append(
                f"line {number}: CMD uses shell form; use the exec form "
                f'(["gunicorn", ...]) so the process receives SIGTERM directly')

    if not any(n == "USER" for n in names):
        problems.append("no USER instruction, so the container runs as root")

    for line in problems:
        print(f"::error::{line}")

    if problems:
        print(f"\n{len(problems)} problem(s) found.")
        return 1

    print(f"Dockerfile OK: {len(instructions)} instructions, "
          f"{len(copied)} copy source(s) all present, runs as a non-root user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
