"""
The password must not reach a log file, a database column, or a public branch.

This file exists because it didn't, and the password did. Four functions in
tools/ turned a connection string into a printable label, and all four
handed the whole string to a parser that divides at the FIRST '@'. Nest a
connection string inside itself -- one careless search-and-replace does it
-- and the first '@' is inside the password, so the parser returns the
username as the host and the rest, password included, as the path.

The label went three places:
    tools/auto_pull.log                   on disk, forever
    replication_state.source_label        into the target database
    manifest.json -> data-export branch   force-pushed to a PUBLIC repo

The nested URL below is the real one, with the password replaced. Every
assertion here is about the same property: whatever goes in, no secret
comes out. A label that is wrong is a cosmetic bug. A label that leaks is
a rotated credential and a rewritten public branch.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import pgconnect  # noqa: E402

SECRET = "DSKv0hRyCxS6JYp0nFtiRYUfUJUQru9L"

HOST = "dpg-db6rqu8n74is738po11g-a.frankfurt-postgres.render.com"

# The shape that actually leaked: the whole URL pasted in where only the
# password belonged, so the string contains itself.
NESTED = (
    f"postgresql://market_db_aktiv_user:"
    f"postgresql://market_db_aktiv_user:{SECRET}@{HOST}/market_db_aktiv"
    f"@{HOST}/market_db_aktiv"
)

LEAKY = [
    pytest.param(NESTED, id="nested-url-the-real-leak"),
    pytest.param(f"postgresql://user:{SECRET}@{HOST}/market_db_aktiv", id="well-formed"),
    pytest.param(f"postgres://user:{SECRET}@{HOST}:5432/db", id="postgres-scheme-and-port"),
    pytest.param(f"postgresql://user:{SECRET}@localhost/db?sslmode=require", id="query-string"),
    pytest.param(f"postgresql://user:pa@{SECRET}@host/db", id="at-sign-inside-password"),
    pytest.param(f"postgresql://user:{SECRET}", id="truncated-no-host"),
    pytest.param(f"{SECRET}", id="not-a-url-at-all"),
    pytest.param(f"postgresql://user:{SECRET}@[::1]:5432/db", id="ipv6-literal"),
    pytest.param(f"postgresql://user:{SECRET}@host/db@evil", id="at-sign-after-dbname"),
    pytest.param(f"  postgresql://user:{SECRET}@host/db  ", id="surrounding-whitespace"),
    # libpq's keyword syntax, which psycopg2 accepts as readily as a URL.
    # No '://' and no '@' anywhere in it, so the URL branch returned the
    # entire string -- password included -- as the "hostname".
    pytest.param(
        f"host={HOST} port=5432 dbname=market_db_aktiv user=u password={SECRET}",
        id="libpq-keyword-dsn",
    ),
    pytest.param(f"dbname=marketdb password='{SECRET}'", id="keyword-dsn-quoted-password"),
    pytest.param(f"password={SECRET}", id="keyword-dsn-password-only"),
]


@pytest.mark.parametrize("url", LEAKY)
def test_the_label_never_contains_the_password(url):
    label = pgconnect.safe_label(url)
    assert SECRET not in label, f"LEAKED: {label!r}"


@pytest.mark.parametrize("url", LEAKY)
def test_the_label_never_contains_credential_punctuation(url):
    """
    A weaker, structural check that holds even for a password this test
    does not know. user:pass@host has an '@' and a ':' in it; a label of
    host/dbname has neither, except the ':' of an explicit port. If either
    survives, something was carried through that should not have been.
    """
    label = pgconnect.safe_label(url)
    assert "@" not in label, f"credential separator survived: {label!r}"
    assert "://" not in label, f"a whole URL survived: {label!r}"


@pytest.mark.parametrize("url", LEAKY)
def test_the_label_is_a_single_short_line(url):
    """Nothing to hide a second line of leaked text in."""
    label = pgconnect.safe_label(url)
    assert "\n" not in label
    assert len(label) <= 120, f"suspiciously long for host/dbname: {label!r}"


def test_a_well_formed_url_still_says_where_it_went():
    """Safety that destroys the label would pass every test above."""
    assert pgconnect.safe_label(
        f"postgresql://user:{SECRET}@{HOST}/market_db_aktiv"
    ) == f"{HOST}/market_db_aktiv"
    assert pgconnect.safe_label(
        "postgresql://postgres:Admin@localhost:5432/marketdb"
    ) == "localhost/marketdb"
    assert pgconnect.safe_label(
        "postgresql://postgres@127.0.0.1:5433/marketdb"
    ) == "127.0.0.1/marketdb"


def test_a_keyword_dsn_still_says_where_it_went():
    """Only host and dbname are read out of it, so nothing else can escape."""
    assert pgconnect.safe_label(
        f"host={HOST} port=5432 dbname=market_db_aktiv user=u password={SECRET}"
    ) == f"{HOST}/market_db_aktiv"
    assert pgconnect.safe_label(
        "host=localhost dbname=marketdb user=postgres"
    ) == "localhost/marketdb"


def test_the_backstop_refuses_rather_than_guesses():
    """
    The last line of safe_label rejects any label still carrying a
    character that cannot appear in a correct host/dbname. Both branches
    above it build the label from named parts, so no input I can construct
    reaches it carrying a secret -- which makes it untestable as a leak
    guard and exactly the kind of code that quietly stops working.

    So its behaviour is pinned here instead: given a label that trips it,
    it must return "?" and not the label. These two inputs hold no
    password; the point is that the mechanism is live. Deleting that line
    makes both of these return the raw value, and this test fail.
    """
    assert pgconnect.safe_label("postgresql://u:p@host/db:extra") == "?"
    assert pgconnect.safe_label("host=a@b dbname=c") == "?"


def test_the_nested_url_still_names_the_right_host():
    """
    Cutting at the LAST '@' is what makes this work, and it happens to
    recover the correct host from the nested string too -- so the fix
    costs no diagnostic value on the exact input that broke it.
    """
    assert pgconnect.safe_label(NESTED) == f"{HOST}/market_db_aktiv"


# --------------------------------------------------------------------------
# Every caller must go through it. A fifth private copy would reintroduce
# the bug without touching a single test above, which is how four copies
# came to exist in the first place.
# --------------------------------------------------------------------------

CALLERS = [
    ("tools.replicate", "_label"),        # -> auto_pull.log, replication_state
    ("tools.export_data", "_host"),       # -> manifest.json, PUBLIC branch
    ("tools.local_setup", "describe"),    # -> console
]


@pytest.mark.parametrize("module_name,function_name", CALLERS)
@pytest.mark.parametrize("url", LEAKY)
def test_no_caller_leaks_either(module_name, function_name, url):
    import importlib

    module = importlib.import_module(module_name)
    label = getattr(module, function_name)(url)
    assert SECRET not in label, f"{module_name}.{function_name} LEAKED: {label!r}"
    assert "@" not in label


def test_nothing_in_tools_parses_a_url_without_cutting_the_credential_first():
    """
    The copies are gone; this keeps them gone. urlsplit and parse_dsn are
    both safe ONLY on a well-formed URL, so outside pgconnect.safe_label
    neither belongs anywhere near a connection string.
    """
    offenders = []
    for path in sorted((REPO / "tools").glob("*.py")):
        if path.name == "pgconnect.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "urlsplit(" in code or "parse_dsn(" in code:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "parse a connection string through pgconnect.safe_label, not directly:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# The pre-flight complaint. Not a leak guard -- a usability one -- but it
# handles the same malformed string, so it is tested in the same place.
# --------------------------------------------------------------------------

def test_the_nested_url_is_rejected_before_connecting():
    wrong = pgconnect.complain_about_url(NESTED)
    assert wrong is not None
    assert "second connection string" in wrong
    assert "secrets.cmd" in wrong


@pytest.mark.parametrize("url", LEAKY)
def test_the_complaint_never_quotes_the_string_it_rejects(url):
    """It is mostly password; printing it is how it got into a log file."""
    wrong = pgconnect.complain_about_url(url)
    if wrong is not None:
        assert SECRET not in wrong, f"LEAKED: {wrong!r}"


@pytest.mark.parametrize("url", [
    f"postgresql://user:{SECRET}@{HOST}/market_db_aktiv",
    "postgresql://postgres:Admin@localhost:5432/marketdb",
    "postgres://u:p@h:5432/db?sslmode=require",
    "host=localhost dbname=marketdb user=postgres password=x",
])
def test_a_usable_connection_string_is_not_complained_about(url):
    """A check that rejects everything would pass every test above."""
    assert pgconnect.complain_about_url(url) is None


# --------------------------------------------------------------------------
# set_secrets: it handles the password, so it gets the same scrutiny.
# --------------------------------------------------------------------------

def test_the_render_url_is_split_into_its_parts():
    from tools.set_secrets import split_url

    bits = split_url(f"postgresql://market_db_aktiv_user:{SECRET}@{HOST}/market_db_aktiv")
    assert bits["user"] == "market_db_aktiv_user"
    assert bits["password"] == SECRET
    assert bits["host"] == HOST
    assert bits["dbname"] == "market_db_aktiv"
    assert bits["port"] == "5432"


def test_sslmode_survives_the_round_trip():
    """Rebuilding without it would silently drop a server requirement."""
    from tools.set_secrets import connection_string, split_url

    url = f"postgresql://u:{SECRET}@{HOST}/market_db_aktiv?sslmode=require"
    bits = split_url(url)
    rebuilt = connection_string(bits["user"], bits["password"], bits["host"],
                                bits["port"], bits["dbname"], bits["query"])
    assert "sslmode=require" in rebuilt
    assert "?" not in connection_string("u", "p", "h", "5432", "db", "")


def test_a_percent_in_a_password_is_doubled_for_cmd():
    """cmd eats '%' in pairs; the old batch version only warned about it."""
    from tools.set_secrets import for_cmd

    assert for_cmd("pa%ss") == "pa%%ss"
    assert for_cmd("p%%q") == "p%%%%q"
    assert for_cmd("ordinary") == "ordinary"


def test_a_percent_encoded_password_is_decoded_once():
    """postgresql://u:pa%40ss@h/db means the password is pa@ss."""
    from tools.set_secrets import split_url

    assert split_url("postgresql://u:pa%40ss@h/db")["password"] == "pa@ss"


@pytest.mark.parametrize("url", LEAKY)
def test_split_url_either_works_or_refuses_without_echoing(url):
    from tools.set_secrets import UrlUnusable, split_url

    try:
        split_url(url)
    except UrlUnusable as exc:
        assert SECRET not in str(exc), f"LEAKED: {exc}"


def test_the_starred_summary_hides_the_password():
    from tools.set_secrets import starred

    line = starred("market_db_aktiv_user", HOST, "5432", "market_db_aktiv")
    assert SECRET not in line
    assert "********" in line


# --------------------------------------------------------------------------
# tidy(): what actually arrives from a console prompt and a clipboard.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_length", [
    ("\x16\x16", 0),            # Ctrl+V inserts control chars, it does not paste
    ("\x16", 0),
    ("\x00\x1b", 0),
    ("", 0),
    ("   ", 0),
    ("\n\n", 0),
])
def test_control_characters_are_not_mistaken_for_a_password(raw, expected_length):
    """
    A prompt once reported "got 2 characters" for a password of 32,
    because Ctrl+V in a console inserts the literal control character
    instead of pasting. Two bytes of junk then looked like success.
    """
    from tools.set_secrets import tidy

    assert len(tidy(raw)) == expected_length


@pytest.mark.parametrize("wrapper", [
    "{url}", "{url}\n", "  {url}  ", "\n\n{url}", '"{url}"', "'{url}'",
    "psql {url}", "psql '{url}'", "{url}\r\n",
])
def test_a_copied_url_survives_however_it_was_copied(wrapper):
    """Render offers the connection as a psql command as well as a URL."""
    from tools.set_secrets import tidy

    url = f"postgresql://u:{SECRET}@{HOST}/market_db_aktiv"
    assert tidy(wrapper.format(url=url)) == url


def test_the_whole_run_writes_a_usable_file(tmp_path, monkeypatch):
    """
    End to end with the console stubbed out, because the person running
    this cannot debug a traceback and every round trip costs them an hour.
    """
    from tools import set_secrets

    secrets = tmp_path / "secrets.cmd"
    monkeypatch.setattr(set_secrets, "SECRETS", secrets)
    monkeypatch.setattr(set_secrets, "check", lambda url, what: True)
    monkeypatch.setattr(
        set_secrets, "clipboard",
        lambda: f"postgresql://market_db_aktiv_user:{SECRET}@{HOST}/market_db_aktiv",
    )

    answers = iter(["", "", "", ""])          # Enter at the URL prompt, then defaults
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(set_secrets.getpass, "getpass", lambda *a: "Admin")

    assert set_secrets.main() == 0

    written = secrets.read_text()
    assert f'set "SOURCE=postgresql://market_db_aktiv_user:{SECRET}@{HOST}:5432/market_db_aktiv"' in written
    assert 'set "TARGET=postgresql://postgres:Admin@localhost:5432/marketdb"' in written
    # Each line must be the whole `set "NAME=..."`, quotes included: pasting
    # a bare URL is what made cmd try to run it.
    for line in written.splitlines():
        if line.startswith("set "):
            assert line.startswith('set "') and line.endswith('"')


def test_the_run_doubles_a_percent_on_the_way_into_the_file(tmp_path, monkeypatch):
    from tools import set_secrets

    secrets = tmp_path / "secrets.cmd"
    monkeypatch.setattr(set_secrets, "SECRETS", secrets)
    monkeypatch.setattr(set_secrets, "check", lambda url, what: True)
    monkeypatch.setattr(set_secrets, "clipboard", lambda: "")

    answers = iter(["skip", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(set_secrets.getpass, "getpass", lambda *a: "pa%ss")

    assert set_secrets.main() == 0
    assert 'set "TARGET=postgresql://postgres:pa%%ss@localhost:5432/marketdb"' \
        in secrets.read_text()
