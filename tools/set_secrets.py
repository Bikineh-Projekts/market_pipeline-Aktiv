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
    """
    Whatever was last copied, or "". Windows only; harmless elsewhere.

    Two ways round, because Get-Clipboard is not available in every
    PowerShell and the .NET call needs a single-threaded apartment that
    PowerShell does not use by default.
    """
    attempts = (
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        ["powershell", "-NoProfile", "-Sta", "-Command",
         "Add-Type -AssemblyName System.Windows.Forms; "
         "[System.Windows.Forms.Clipboard]::GetText()"],
    )
    for command in attempts:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        text = (done.stdout or "").strip()
        if text:
            return text
    return ""


def ask(label: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    answer = input(f"   {label}{shown}: ").strip()
    return answer or default


def tidy(value: str) -> str:
    """
    The first real line of something copied, with the junk taken off.

    Ctrl+V does not paste into a console prompt -- it inserts the literal
    control character, which is how a prompt once reported "got 2
    characters" for what should have been thirty-two. Those get stripped
    here rather than written into a file.

    Render also offers the connection as a ready-made psql command, and
    copying that instead of the URL is an easy and invisible mistake, so
    the prefix and any surrounding quotes come off too.
    """
    line = ""
    for candidate in value.splitlines():
        if candidate.strip():
            line = candidate.strip()
            break

    line = "".join(ch for ch in line if ch.isprintable())
    if line.lower().startswith("psql "):
        line = line[5:].strip()
    return line.strip().strip("'\"").strip()


def ask_secret(label: str, hint: str = "") -> str:
    """
    A short value, typed. Long ones come from the clipboard instead.

    The previous version offered typing, pasting, or the clipboard, with
    the clipboard behind an empty Enter. That was the wrong way round:
    Ctrl+V inserts a control character rather than pasting, so the one
    route that cannot work looked exactly like the one that does, and the
    prompt reported success for two junk characters.
    """
    print(f"\n   {label}")
    if hint:
        print(f"     {hint}")
    print("     Nothing appears as you type -- that is normal.")
    print("     Press Enter on its own to use whatever you last COPIED.")
    try:
        typed = getpass.getpass("     > ")
    except (EOFError, KeyboardInterrupt):
        return ""

    typed = tidy(typed)
    if typed:
        print(f"     got {len(typed)} characters.")
        return typed

    pasted = tidy(clipboard())
    if pasted:
        print(f"     took {len(pasted)} characters from the clipboard.")
        return pasted

    print("     nothing typed, and nothing on the clipboard.")
    return ""


def ask_url_from_clipboard() -> str:
    """
    The Render URL, via the clipboard, with nothing typed at all.

    This one is never typed and never pasted into a prompt. It is 100-odd
    characters of which 32 are random, so copying it is the only sane way
    in -- which means the clipboard is the primary route here, not the
    fallback, and the prompt is a plain Enter that shows what it found.
    """
    while True:
        answer = input(
            "\n   Copy the External Database URL from Render, then press Enter.\n"
            "   (or type  skip  to use the published export instead): "
        ).strip().lower()

        if answer in ("skip", "s", "n", "no"):
            return ""

        raw = tidy(clipboard())
        if not raw:
            print("\n     The clipboard is empty. In Render: open the database,")
            print("     find \"External Database URL\", click the copy button.")
            continue

        wrong = pgconnect.complain_about_url(raw)
        if wrong:
            # Shown, not written: complain_about_url never quotes the
            # string it rejects, because that string is mostly password.
            print(f"\n     What you copied is not usable: {wrong}")
            print("\n     Copy the External Database URL again -- the whole")
            print("     thing, in one piece -- and press Enter.")
            continue

        return raw


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
    print("\n   The long password is never typed. Copy it, and this reads it")
    print("   from the clipboard.\n")

    # --- 1. Render, FIRST --------------------------------------------------
    # Before the local password, deliberately: a clipboard holds one thing
    # at a time, and this is the value that cannot be typed -- a hundred
    # characters of which thirty-two are random. The local password is
    # short and known, so it is the one that can wait.
    print("   [1 of 2] RENDER.")
    print("   Open the database in the Render dashboard and click the copy")
    print('   button on "External Database URL".')

    raw = ask_url_from_clipboard()
    source = None
    if raw:
        bits = split_url(raw)   # already validated inside ask_url_from_clipboard
        source = connection_string(
            bits["user"], bits["password"], bits["host"], bits["port"],
            bits["dbname"], bits["query"],
        )
        print("\n     Read from what you copied:")
        print(f"       host     {bits['host']}")
        print(f"       user     {bits['user']}")
        print(f"       database {bits['dbname']}")
        print(f"       password {'*' * 8}  ({len(bits['password'])} characters)")
    else:
        print("\n     Skipping Render. Imports will come from the published")
        print("     export, which needs no Render credentials and keeps")
        print("     working after Render deletes the free database at 30 days.")

    # --- 2. the local database --------------------------------------------
    print("\n   [2 of 2] YOUR OWN PostgreSQL -- the one pgAdmin connects to.")
    local_user = ask("user name", "postgres")
    local_db = ask("database", "marketdb")
    local_port = ask("port", "5432")
    local_pass = ask_secret(
        "Its password:",
        "This one is short enough to type -- it is the password you set when "
        "you installed PostgreSQL.",
    )
    if not local_pass:
        print("\n   No password. Nothing was written.\n")
        return 1

    target = connection_string(local_user, local_pass, "localhost", local_port, local_db)

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
