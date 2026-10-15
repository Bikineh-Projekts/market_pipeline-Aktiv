#!/usr/bin/env python3
"""
Write tools/secrets.cmd without anyone having to type a password.

WHY THIS EXISTS
---------------
The Render password is 32 random characters. Asking someone to retype it
is asking for a typo that surfaces three steps later as a connection
failure, and asking them to paste it into a masked prompt is asking them
to trust that something happened when the screen showed nothing.

So nothing has to be typed. At any password prompt, press Enter on its own
and the value is taken from the clipboard instead.

And at the Render prompt the whole External Database URL can go in --
the one string Render's dashboard gives you to copy. Host, user, database
and password are pulled out of it here. That removes the step that has
gone wrong every single time: a human splitting that URL into parts, or
pasting the whole thing into the slot where only the password belonged.

NOTHING IS EVER PRINTED
-----------------------
Passwords are echoed back as stars, and the summary at the end shows the
connection strings with the credentials starred out. The first version of
this setup put a password in a log file; the way that gets repeated is by
printing one "just to check".

ONE CHARACTER CMD STILL EATS
----------------------------
A '%' in a password has to be doubled in a .cmd file, or cmd swallows it
in pairs. The old batch version printed a warning about this and left the
doubling to the reader. This does it.
"""

from __future__ import annotations

import getpass
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import pgconnect  # noqa: E402

SECRETS = pathlib.Path(__file__).resolve().parent / "secrets.cmd"


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def clipboard() -> str:
    """Whatever was last copied, or ''. Windows only; harmless elsewhere."""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=20,
        )
        return done.stdout.strip()
    except Exception:
        return ""


def ask(label: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    answer = input(f"   {label}{shown}: ").strip()
    return answer or default


def ask_secret(label: str) -> str:
    """
    Typed, pasted, or taken from the clipboard. Never shown.

    getpass accepts a paste perfectly well -- right-click in a console
    window, or Ctrl+V -- but it shows nothing while you do it, so there is
    no way to tell a successful paste from a dead keystroke. The Enter
    route exists so that uncertainty never has to be resolved.
    """
    print(f"\n   {label}")
    print("     paste it (right-click or Ctrl+V -- the screen stays blank,")
    print("     that is normal), or just press Enter to take it from the")
    print("     clipboard.")
    try:
        value = getpass.getpass("     > ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""

    if value:
        print(f"     got {len(value)} characters.")
        return value

    value = clipboard()
    if value:
        first_line = value.splitlines()[0].strip() if value else ""
        print(f"     took {len(first_line)} characters from the clipboard.")
        return first_line

    print("     nothing typed and the clipboard is empty.")
    return ""


# --------------------------------------------------------------------------
# the Render URL
# --------------------------------------------------------------------------

# Parsing lives in pgconnect, which is the only module allowed to take a
# connection string apart -- see pgconnect.split_url for why.
UrlUnusable = pgconnect.UrlUnusable
split_url = pgconnect.split_url


# --------------------------------------------------------------------------
# writing it
# --------------------------------------------------------------------------

def for_cmd(value: str) -> str:
    """
    A value as it must appear inside set "NAME=value" in a .cmd file.

    Only '%' needs doing. Everything else that cmd treats specially --
    & | < > ^ -- is inert inside the quotes, which is the whole reason
    every line is written as set "NAME=value" rather than set NAME=value.
    """
    return value.replace("%", "%%")


def connection_string(user: str, password: str, host: str, port: str,
                      dbname: str, query: str = "") -> str:
    tail = f"?{query}" if query else ""
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}{tail}"


def starred(user: str, host: str, port: str, dbname: str, query: str = "") -> str:
    tail = f"?{query}" if query else ""
    return f"postgresql://{user}:{'*' * 8}@{host}:{port}/{dbname}{tail}"


def write(target: str, source: str | None) -> None:
    lines = [
        "@rem Written by set_secrets.py. Never commit this file.",
        f'set "TARGET={for_cmd(target)}"',
    ]
    if source:
        lines.append(f'set "SOURCE={for_cmd(source)}"')
    SECRETS.write_text("\r\n".join(lines) + "\r\n", encoding="ascii", errors="strict")


# --------------------------------------------------------------------------
# checking it, before anything is scheduled around it
# --------------------------------------------------------------------------

def check(url: str, what: str) -> bool:
    print(f"\n   testing {what}: {pgconnect.safe_label(url)}")
    try:
        conn = pgconnect.connect(url, timeout=20)
    except pgconnect.ConnectionFailed as exc:
        first = str(exc).strip().splitlines()[0]
        print(f"     FAILED  {first}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"     FAILED  {type(exc).__name__}: {exc}")
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        name = cur.fetchone()[0]
    conn.close()
    print(f"     ok      connected to {name}")
    return True


def main() -> int:
    print("\n   " + "=" * 58)
    print("    Writing tools\\secrets.cmd")
    print("   " + "=" * 58)
    print("\n   Nothing has to be typed. At every password prompt you can")
    print("   press Enter on its own and the value is taken from whatever")
    print("   you last copied.\n")

    # --- 1. the local database -------------------------------------------
    print("   [1 of 2] YOUR OWN PostgreSQL -- the one pgAdmin connects to.")
    local_user = ask("user name", "postgres")
    local_db = ask("database", "marketdb")
    local_port = ask("port", "5432")
    local_pass = ask_secret("Its password:")
    if not local_pass:
        print("\n   No password. Nothing was written.\n")
        return 1

    target = connection_string(local_user, local_pass, "localhost", local_port, local_db)

    # --- 2. Render --------------------------------------------------------
    print("\n   [2 of 2] RENDER.")
    print("   In the Render dashboard, open the database and copy")
    print('   "External Database URL" -- the WHOLE thing, in one piece.')
    print("   Then press Enter at the prompt below and it is read from")
    print("   the clipboard; host, user, database and password are taken")
    print("   out of it here, so there is nothing to split up by hand.")
    print("\n   Or press Enter with an empty clipboard to skip Render")
    print("   entirely: the task then imports from the published export,")
    print("   which needs no Render credentials and keeps working after")
    print("   Render deletes the free database at 30 days.")

    raw = ask_secret("The External Database URL:")
    source = None
    if raw:
        try:
            bits = split_url(raw)
        except UrlUnusable as exc:
            print(f"\n   That is not usable: {exc}\n")
            print("   Nothing was written. Copy the External Database URL")
            print("   again and run this once more.\n")
            return 1
        source = connection_string(
            bits["user"], bits["password"], bits["host"], bits["port"],
            bits["dbname"], bits["query"],
        )
        print(f"     host     {bits['host']}")
        print(f"     user     {bits['user']}")
        print(f"     database {bits['dbname']}")
        print(f"     password {'*' * 8}  ({len(bits['password'])} characters)")
    else:
        print("\n     No Render URL. Imports will come from the published export.")

    # --- write and verify -------------------------------------------------
    try:
        write(target, source)
    except UnicodeEncodeError:
        print("\n   One of those values has a non-ASCII character in it, which")
        print("   a .cmd file cannot carry reliably. Change the password to")
        print("   plain letters and digits and run this again.\n")
        return 1

    print(f"\n   Written: {SECRETS}")
    print("\n   It contains, with the passwords hidden:")
    print(f"     set \"TARGET={starred(local_user, 'localhost', local_port, local_db)}\"")
    if source:
        bits = split_url(source)
        print(f"     set \"SOURCE={starred(bits['user'], bits['host'], bits['port'], bits['dbname'], bits['query'])}\"")

    if "%" in local_pass or (source and "%" in source):
        print("\n   A password contains a '%'. It has been doubled in the file,")
        print("   which is what cmd needs -- that is correct, not a mistake.")

    # Connecting now, rather than letting a wrong password turn up in a log
    # file in fifteen minutes' time.
    ok = check(target, "your own database")
    if source:
        ok = check(source, "Render") and ok

    if not ok:
        print("\n   The file was written, but something above did not connect.")
        print("   Fix that first -- scheduling a task that fails every 15")
        print("   minutes only fills a log.\n")
        return 1

    print("\n   Both connected. Next:")
    print("     tools\\auto_pull.cmd            one import now")
    print("     tools\\install_auto_pull.cmd    then every 15 minutes\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n   Stopped. Nothing was written.\n")
        sys.exit(130)
