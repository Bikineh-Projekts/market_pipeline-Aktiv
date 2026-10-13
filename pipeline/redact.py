"""
Remove secrets from text before it is stored or displayed.

WHY THIS EXISTS
---------------
Error messages are not neutral text. `requests` puts the full request URL
into the string form of a connection error, and for two of the three
providers here the API key travels in the query string:

    ConnectionError: HTTPSConnectionPool(host='api.twelvedata.com',
    port=443): Max retries exceeded with url: /time_series?symbol=AAPL
    &interval=1day&apikey=REAL_KEY_HERE (Caused by ...)

That string was being written to log_api_call.error_msg, which means the
key was sitting in the database -- and from there into the dashboard, the
Power BI model, the CSV export and, once the export is published, a public
branch. The same applies to psycopg2: a connection failure can put the
whole DSN into its message.

Nothing downstream can undo this. A key that has been published has to be
rotated at the provider, so it has to be removed before it is stored, not
before it is shown.

HOW
---
Two independent layers, because either alone has a hole:

  1. The configured secret VALUES are replaced wherever they appear. This
     catches a key however it got into the string -- a parameter name we
     did not anticipate, a provider that echoes it back in a body, a
     header dumped into a traceback.

  2. Known secret-bearing PARAMETERS and connection-string passwords are
     matched by shape. This catches a credential this process does not
     itself hold: a key passed in a URL built elsewhere, or a DSN for a
     database other than ours.
"""

from __future__ import annotations

import os
import re

MASK = "***"

# apikey=X, api_key=X, token=X, access_token=X, key=X, password=X, pwd=X
# ...in a query string or a key=value pair. The value runs to the next
# separator; `&`, whitespace, quotes and a closing bracket all end it.
_PARAM = re.compile(
    r"((?:api[-_]?key|access[-_]?token|auth[-_]?token|token|apikey|key|"
    r"password|passwd|pwd|secret)"
    r"\s*[=:]\s*)"
    r"([^&\s'\"<>|,;)\]}]+)",
    re.IGNORECASE,
)

# The password in a connection string: scheme://user:PASSWORD@host
_DSN = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:/@\s]+:)"
    r"([^@\s]+)(@)",
    re.IGNORECASE,
)

# Bearer <token>
_BEARER = re.compile(r"((?:Bearer|Basic)\s+)([A-Za-z0-9._\-+/=]{8,})",
                     re.IGNORECASE)

# Env vars whose values must never appear in text. Read at call time, not
# at import, so a test that sets one is honoured and a value that arrives
# late is still covered.
_SECRET_ENV = (
    "FINNHUB_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "TWELVEDATA_API_KEY",
    "DATABASE_URL",
    "DATABASE_URL_DIRECT",
    "PGPASSWORD",
    "DATABRICKS_CLIENT_SECRET",
    "RENDER_API_KEY",
    "GITHUB_TOKEN",
)

# Below this length a "secret" is more likely to be a placeholder than a
# credential, and masking it would turn ordinary words into *** -- which
# makes real errors unreadable without protecting anything. The test keys
# in the suite are longer than this on purpose.
_MIN_SECRET_LEN = 6


def secret_values() -> list[str]:
    """The literal secrets this process holds, longest first."""
    values: set[str] = set()
    for name in _SECRET_ENV:
        raw = (os.getenv(name) or "").strip()
        if len(raw) < _MIN_SECRET_LEN:
            continue
        values.add(raw)
        # A DSN also leaks as its parts: masking the whole string does not
        # help when only the password appears.
        if "://" in raw:
            match = _DSN.search(raw)
            if match and len(match.group(2)) >= _MIN_SECRET_LEN:
                values.add(match.group(2))
    # Longest first, so masking a short value cannot break a longer one
    # that contains it.
    return sorted(values, key=len, reverse=True)


def scrub(text: str | None, limit: int | None = None) -> str | None:
    """
    `text` with every credential this module can recognise replaced.

    Returns None for None, so it can wrap a nullable column's value
    without the caller having to check first.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)

    for value in secret_values():
        text = text.replace(value, MASK)

    text = _DSN.sub(rf"\1{MASK}\3", text)
    text = _PARAM.sub(rf"\1{MASK}", text)
    text = _BEARER.sub(rf"\1{MASK}", text)

    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text
