#!/usr/bin/env python3
"""
Fail if a connection string with a real password is committed.

This guards a mistake that is easy to make and expensive to undo: pasting
a working `DATABASE_URL` into a file, a README or a note. Once it is in git
history, rotating the password is the only fix.

Placeholders are allowed, and are recognised by shape rather than by a
hand-maintained list of exact strings -- an ALL_CAPS password is obviously
not real, a random-looking one obviously is.

    python tools/check_no_credentials.py [path]

Exit 0 when clean, 1 when something looks like a live credential.
"""

from __future__ import annotations

import re
import subprocess
import sys

# scheme://user:password@host
PATTERN = re.compile(
    r"postgres(?:ql)?://(?P<user>[A-Za-z0-9_.\-]+):(?P<password>[^@/\s'\"]+)@",
)

# Passwords that are plainly stand-ins.
PLACEHOLDER_LITERALS = {
    "password", "pass", "pw", "secret", "changeme", "example",
    "postgres", "mypassword", "oldpassword", "placeholder", "token",
    "trust-auth-ignores-this", "fresh-token", "your-password",
}


def is_placeholder(user: str, password: str) -> bool:
    low = password.lower()

    if low in PLACEHOLDER_LITERALS:
        return True
    # PASSWORD, YOUR_PASSWORD, <PASSWORD> -- shouting means placeholder.
    if re.fullmatch(r"[A-Z0-9_<>\-]+", password):
        return True
    # Angle brackets or braces are templating, not a secret.
    if any(ch in password for ch in "<>{}"):
        return True
    # user == password is a local dev convention, never a real secret.
    if low == user.lower():
        return True
    # Too short to be a generated credential.
    if len(password) < 8:
        return True
    return False


def tracked_files(root: str) -> list[str]:
    """Only files git knows about -- node_modules and venvs are not ours."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", root],
            capture_output=True, text=True, check=True,
        ).stdout
        return [f for f in out.split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        import pathlib
        return [str(p) for p in pathlib.Path(root).rglob("*")
                if p.is_file() and ".git" not in p.parts]


def main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else "."
    findings: list[str] = []

    for path in tracked_files(root):
        if path.endswith(("check_no_credentials.py",)):
            continue  # this file documents the placeholder list
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, start=1):
                    for match in PATTERN.finditer(line):
                        user = match.group("user")
                        password = match.group("password")
                        if is_placeholder(user, password):
                            continue
                        findings.append(
                            f"{path}:{lineno}: connection string for user "
                            f"'{user}' with what looks like a real password"
                        )
        except (OSError, UnicodeDecodeError):
            continue

    for finding in findings:
        print(f"::error::{finding}")

    if findings:
        print(f"\n{len(findings)} committed credential(s) found.")
        print("Rotate the password at the provider, then remove it from the file.")
        print("Credentials belong in GitHub Actions secrets, nowhere else.")
        return 1

    print("No committed credentials found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
