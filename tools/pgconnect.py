"""
Connecting, on a machine whose PostgreSQL does not speak English.

A German PostgreSQL reports

    FATAL:  Passwort-Authentifizierung für Benutzer »postgres« fehlgeschlagen

and those guillemets are 0xbb and 0xab in the Windows code page, which are
not valid UTF-8 on their own. psycopg2 decodes the server's message as
UTF-8, so instead of the reason you get

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb in position 79

which names no host, no database and no cause. The connection failed for
an ordinary reason -- a wrong password, a misspelt database -- and the
only part that reached you was the failure to render it.

Every tool here connects through connect(), so a bad password reads as a
bad password on a German, French or Spanish Windows too. The raw bytes
survive on the exception as .object, which is what makes this recoverable
without touching the server.

The permanent fix belongs on the server and is worth doing anyway:
postgresql.conf, lc_messages = 'C', restart. Then PostgreSQL reports in
English and nothing has to be rescued. This module is for every machine
where that has not been done.
"""

from __future__ import annotations

import psycopg2

# Tried in order. latin-1 never raises, so it is last and always succeeds --
# a message with a few wrong characters beats no message at all.
_FALLBACKS = ("utf-8", "cp1252", "cp1251", "latin-1")


def decode_server_bytes(raw: bytes) -> str:
    for encoding in _FALLBACKS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return repr(raw)


def safe_label(url: str) -> str:
    """
    host/dbname out of a connection string, with no credential in it. Ever.

    This is the only function in the project allowed to turn a connection
    string into something printable, because four separate copies of it
    existed and all four were wrong in the same way.

    Each one handed the whole URL to a parser -- urlsplit in two, libpq's
    parse_dsn in two -- and both parsers divide at the FIRST '@'. On a
    well-formed URL that is the right '@'. On a connection string that has
    been nested inside itself, which a search-and-replace produces in one
    keystroke, the first '@' sits in the middle of the password, so the
    username came back as the hostname and everything after it, PASSWORD
    INCLUDED, came back as the path or the dbname. The label was then
    written to auto_pull.log, to replication_state.source_label, and -- in
    export_data -- into a manifest.json that a workflow force-pushes to a
    public branch of a public repository.

    So the credential is cut off by hand, at the LAST '@', before any
    parser is given the chance to interpret it. A label exists only to be
    read by a human: being approximate costs nothing, and leaking cannot
    be taken back.
    """
    from urllib.parse import urlsplit

    text = url.strip()

    # libpq's OTHER syntax, which psycopg2 accepts just as happily:
    #     host=x port=5432 dbname=y user=u password=SECRET
    # It contains no '://' and no '@', so the URL path below treated the
    # whole thing as a hostname and handed back the password inside it.
    # Here only two keys are ever read, so there is nothing else to emit.
    if "://" not in text and "=" in text:
        fields: dict[str, str] = {}
        for token in text.split():
            key, _, value = token.partition("=")
            if key.strip() in ("host", "hostaddr", "dbname"):
                fields[key.strip()] = value.strip().strip("'\"")
        host = fields.get("host") or fields.get("hostaddr") or "?"
        dbname = fields.get("dbname", "")
        label = f"{host}/{dbname}" if dbname else host
    else:
        # rsplit, not split: a password may legitimately contain '@', and
        # the separator that matters is the LAST one.
        after_credentials = text.split("://", 1)[-1].rsplit("@", 1)[-1]
        try:
            parts = urlsplit("//" + after_credentials)
            host, path, port = parts.hostname or "?", parts.path or "", parts.port
        except ValueError:
            return "?"
        label = f"{host}{path}"
        # The ':' of an explicit port is the only one that belongs here.
        if port:
            label = label.replace(f":{port}", "", 1)

    # The backstop. Everything above builds the label out of named parts,
    # so none of these characters can appear in a correct one -- if one
    # does, some input found a path through that was not anticipated, and
    # saying nothing is the right answer.
    if any(c in label for c in "@=:'\" \t\n"):
        return "?"
    return label


def complain_about_url(url: str) -> str | None:
    """
    The one-line reason a connection string is malformed, or None.

    Checked BEFORE connecting, because libpq's own report of the commonest
    mistake here is unusable. Paste a whole connection string in where only
    the password belonged -- which is what a search-and-replace does if the
    replacement is the URL instead of the password -- and libpq says

        invalid integer value "postgresql:" for connection option "port"

    It is talking about a port nobody typed, it does not mention the file
    the string came from, and it gives no hint that the string now contains
    itself. Three separate debugging rounds went into that message.

    The returned text names the file and what to put in it. It never
    contains the string it is complaining about: that string is mostly
    password.
    """
    text = url.strip()
    if not text:
        return "the connection string is empty."

    if text.count("://") > 1:
        return (
            "the connection string contains a second connection string inside it.\n"
            "    This happens when a whole URL is pasted in where only the password\n"
            "    belonged. tools\\secrets.cmd should hold ONE url per line:\n"
            '      set "SOURCE=postgresql://USER:PASSWORD@HOST/DBNAME"\n'
            "    with PASSWORD replaced by the password on its own -- not by a url."
        )

    if "://" not in text and "=" not in text:
        return (
            "that is not a connection string.\n"
            "    It must start with postgresql:// -- and the line in\n"
            '    tools\\secrets.cmd must be the whole `set "NAME=..."`, not the url alone.'
        )

    if "://" in text:
        after_credentials = text.split("://", 1)[-1].rsplit("@", 1)[-1]
        if not after_credentials or after_credentials.startswith("/"):
            return "the connection string has no host in it."

    return None


class UrlUnusable(Exception):
    """A connection string that cannot be taken apart, with the reason."""


def split_url(url: str) -> dict[str, str]:
    """
    user / password / host / port / dbname / query out of a connection URL.

    This lives here, beside safe_label, because this module is the only
    place allowed to parse a connection string -- a rule a test enforces.
    Four private copies of a URL-parsing helper is how the password got
    into a log file, and an exception list would have let a fifth back in.

    complain_about_url runs first, so the nested string that broke this
    three times is refused with an explanation instead of being parsed
    into nonsense.
    """
    from urllib.parse import unquote, urlsplit

    wrong = complain_about_url(url)
    if wrong:
        raise UrlUnusable(wrong)

    parts = urlsplit(url.strip())
    if not parts.hostname:
        raise UrlUnusable("that URL has no host in it.")
    try:
        port = parts.port
    except ValueError:
        raise UrlUnusable(
            "that URL has something other than a number where the port goes."
        ) from None

    return {
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "host": parts.hostname,
        "port": str(port or 5432),
        "dbname": parts.path.lstrip("/"),
        # Render's dashboard sometimes hands the URL out with
        # ?sslmode=require on the end. Rebuilding without it would quietly
        # drop a requirement the server may enforce, so it is carried.
        "query": parts.query,
    }


class ConnectionFailed(Exception):
    """A connection error whose message survived a non-UTF-8 server."""


def connect_raw(url: str, *, timeout: int = 15, **kwargs):
    """connect(), for callers that set their own autocommit and options."""
    try:
        return psycopg2.connect(url, connect_timeout=timeout, **kwargs)
    except UnicodeDecodeError as exc:
        text = decode_server_bytes(bytes(exc.object)).strip()
        raise ConnectionFailed(
            f"{text}\n"
            f"    (the server reported this in its own language; "
            f"set lc_messages = 'C' in postgresql.conf to get English)"
        ) from exc
    except psycopg2.Error as exc:
        raise ConnectionFailed(str(exc).strip()) from exc


def connect(url: str, *, timeout: int = 15, autocommit: bool = False):
    """
    psycopg2.connect, with the server's own words preserved.

    Raises ConnectionFailed carrying a readable message; the caller prints
    it rather than unpacking a traceback.
    """
    try:
        conn = psycopg2.connect(url, connect_timeout=timeout)
    except UnicodeDecodeError as exc:
        text = decode_server_bytes(bytes(exc.object)).strip()
        raise ConnectionFailed(
            f"{text}\n"
            f"    (the server reported this in its own language; "
            f"set lc_messages = 'C' in postgresql.conf to get English)"
        ) from exc
    except psycopg2.Error as exc:
        raise ConnectionFailed(str(exc).strip()) from exc

    if autocommit:
        conn.autocommit = True
    return conn
