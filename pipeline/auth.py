"""
Databricks Lakebase OAuth credentials.

Only used when the four DATABRICKS_* / LAKEBASE_* settings are present.
With a native Postgres password role -- the recommended setup, because
those passwords do not expire -- none of this runs.

Why it exists anyway: a Lakebase OAuth token is valid for 60 minutes, so a
`DATABASE_URL` secret containing one stops working within the hour, and a
backfill that runs longer than an hour would die halfway through even if
the token was fresh at the start. This module mints a token on demand and
refreshes it before expiry, so neither happens.

Getting a database credential takes two calls:

  1. POST {host}/oidc/v1/token         service principal -> workspace token
  2. POST {host}/api/2.0/postgres/credentials   workspace token -> db token

Both last 60 minutes.
"""

from __future__ import annotations

import base64
import logging
import time
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from . import config

logger = logging.getLogger(__name__)

_token: str | None = None
_token_expires_at: float = 0.0


class AuthError(Exception):
    """Could not obtain a database credential."""


def reset_cache() -> None:
    """Forget the cached token. Used by tests and after an auth failure."""
    global _token, _token_expires_at
    _token = None
    _token_expires_at = 0.0


def token_is_fresh() -> bool:
    """True if the cached token will outlive the refresh margin."""
    return bool(_token) and time.time() < _token_expires_at - config.TOKEN_REFRESH_MARGIN


def _workspace_token() -> str:
    """Exchange the service principal's secret for a workspace OAuth token."""
    basic = base64.b64encode(
        f"{config.DATABRICKS_CLIENT_ID}:{config.DATABRICKS_CLIENT_SECRET}".encode()
    ).decode()

    try:
        response = requests.post(
            f"{config.DATABRICKS_HOST}/oidc/v1/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": "all-apis"},
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AuthError(f"could not reach the Databricks OIDC endpoint: {exc}") from exc

    if response.status_code != 200:
        raise AuthError(
            f"workspace token request failed with HTTP {response.status_code}. "
            "Check DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET."
        )

    body = response.json()
    if "access_token" not in body:
        raise AuthError("workspace token response contained no access_token")
    return body["access_token"]


def database_token() -> str:
    """
    A Postgres password valid for about an hour, cached until it nears expiry.

    Raises AuthError with a message that says which setting to look at,
    because this runs unattended and a bare 401 explains nothing.
    """
    global _token, _token_expires_at

    if token_is_fresh():
        return _token  # type: ignore[return-value]

    workspace = _workspace_token()

    try:
        response = requests.post(
            f"{config.DATABRICKS_HOST}/api/2.0/postgres/credentials",
            headers={
                "Authorization": f"Bearer {workspace}",
                "Content-Type": "application/json",
            },
            json={"endpoint": config.LAKEBASE_ENDPOINT},
            timeout=config.HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AuthError(f"could not reach the Lakebase credentials API: {exc}") from exc

    if response.status_code != 200:
        raise AuthError(
            f"database credential request failed with HTTP {response.status_code}. "
            f"Check LAKEBASE_ENDPOINT ({config.LAKEBASE_ENDPOINT or 'unset'}) and that "
            "the service principal has a Postgres role on that branch."
        )

    body = response.json()
    token = body.get("token")
    if not token:
        raise AuthError("database credential response contained no token")

    _token = token
    # Prefer the server's own expiry when it parses; otherwise assume the
    # documented 60 minutes. Never trust a longer lifetime than that.
    _token_expires_at = _parse_expiry(body.get("expire_time")) or (time.time() + 3600)

    remaining = int(_token_expires_at - time.time())
    logger.info("minted a Lakebase database credential, valid for ~%ds", remaining)
    return token


def _parse_expiry(value) -> float | None:
    """RFC 3339 timestamp -> epoch seconds, or None if it cannot be read."""
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime

    text = value.replace("Z", "+00:00")
    # Python cannot parse more than 6 fractional-second digits.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        offset = tail[len(digits):] if not tail[len(digits):].isdigit() else ""
        for marker in ("+", "-", "Z"):
            if marker in tail:
                offset = tail[tail.index(marker):]
                break
        text = f"{head}.{digits}{offset}" if digits else f"{head}{offset}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        logger.warning("could not parse token expiry %r, assuming one hour", value)
        return None


def apply_token(url: str, token: str, user: str | None = None) -> str:
    """
    Return `url` with its password replaced by `token`.

    Keeping the host, port, database and query string from DATABASE_URL
    means OAuth needs no second copy of the connection details -- the
    credential is the only part that changes.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        raise AuthError(f"DATABASE_URL has no host: {url[:40]}...")

    username = user or parts.username or config.DATABRICKS_CLIENT_ID
    if not username:
        raise AuthError("no database username: set it in DATABASE_URL or DATABRICKS_CLIENT_ID")

    # safe="" so a token containing / or : is encoded rather than breaking
    # the URL, and the host is rebuilt rather than string-replaced.
    netloc = f"{quote(username, safe='')}:{quote(token, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
