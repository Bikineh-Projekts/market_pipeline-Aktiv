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


class ConnectionFailed(Exception):
    """A connection error whose message survived a non-UTF-8 server."""


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
