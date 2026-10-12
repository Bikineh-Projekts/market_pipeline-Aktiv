"""
Shared HTTP layer for all collectors.

Every request goes through `fetch`, so every request lands in log_api_call
with its real status code and latency — success or failure. That is what
makes the health monitor's success-rate number mean anything.
"""

from __future__ import annotations

import logging
import time

import requests

from .. import config, db

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """A provider call that did not return usable data."""


class RateLimited(ApiError):
    """The provider refused because a quota or rate limit was reached."""


def fetch(
    source: str,
    url: str,
    endpoint: str,
    params: dict | None = None,
    headers: dict | None = None,
    symbol: str | None = None,
    run_id: int | None = None,
) -> dict:
    """
    GET a JSON endpoint with retries, logging every attempt.

    A market data provider returning 502 for two seconds should not cost a
    symbol its whole update, so transient failures (timeouts, connection
    errors, 5xx) are retried with exponential backoff. Rate limits and 4xx
    responses are not retried: the answer will not change, and on Alpha
    Vantage's 25-requests-per-day budget a retry is pure waste.

    Every attempt is logged separately, so log_api_call shows the real
    failure rate rather than only the final outcome.
    """
    attempts = max(1, config.HTTP_RETRIES + 1)
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return _fetch_once(source, url, endpoint, params, headers, symbol, run_id, attempt)
        except RateLimited:
            raise  # never retry a quota
        except ApiError as exc:
            last = exc
            if not getattr(exc, "retryable", False) or attempt == attempts:
                raise
            delay = config.HTTP_BACKOFF * (2 ** (attempt - 1))
            logger.warning("%s %s failed (attempt %d/%d), retrying in %.1fs: %s",
                           source, endpoint, attempt, attempts, delay, exc)
            time.sleep(delay)

    raise last if last else ApiError(f"{source} {endpoint}: unreachable")


def _fetch_once(
    source: str,
    url: str,
    endpoint: str,
    params: dict | None = None,
    headers: dict | None = None,
    symbol: str | None = None,
    run_id: int | None = None,
    attempt: int = 1,
) -> dict:
    """
    One GET. Logs the attempt, then returns the body or raises.

    Exceptions carry a `retryable` flag that `fetch` reads to decide whether
    another attempt is worth making.
    """
    started = time.perf_counter()
    status: int | None = None
    error: str | None = None

    try:
        response = requests.get(
            url,
            params=params or {},
            headers=headers or {},
            timeout=config.HTTP_TIMEOUT,
        )
        status = response.status_code

        if status == 429:
            error = "rate limit exceeded"
            raise RateLimited(f"{source} rate limit hit on {endpoint}")

        if status >= 500:
            error = f"HTTP {status}"
            exc = ApiError(f"{source} {endpoint}: server returned {status}")
            exc.retryable = True
            raise exc

        response.raise_for_status()
        body = response.json()

        # Providers often return HTTP 200 with an error inside the body.
        provider_error = _provider_error(body)
        if provider_error:
            error = provider_error
            if _is_quota_message(provider_error):
                raise RateLimited(f"{source} {endpoint}: {provider_error}")
            raise ApiError(f"{source} {endpoint}: {provider_error}")

        return body

    except ApiError:
        raise

    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError) as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        wrapped = ApiError(f"{source} {endpoint}: {error}")
        wrapped.retryable = True
        raise wrapped from exc

    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise ApiError(f"{source} {endpoint}: {error}") from exc

    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        db.log_api_call(
            source=source,
            symbol=symbol,
            # Mark retries so a burst of logged failures is recognisable as
            # one flaky call rather than several independent ones.
            endpoint=endpoint if attempt == 1 else f"{endpoint} (retry {attempt - 1})",
            http_status=status,
            response_ms=elapsed_ms,
            error=error,
            run_id=run_id,
        )


# Phrases providers use when a quota is spent. Retrying these is pointless
# and, on Alpha Vantage's 25-per-day budget, actively harmful.
_QUOTA_PHRASES = (
    "rate limit",
    "requests per day",
    "requests per minute",
    "api call frequency",
    "quota",
    "upgrade your plan",
    "premium endpoint",
    "run out of api credits",
)


def _is_quota_message(message: str) -> bool:
    low = message.lower()
    return any(p in low for p in _QUOTA_PHRASES)


def _provider_error(body) -> str | None:
    """Detect an error reported inside a 200-OK response body."""
    if not isinstance(body, dict):
        return None

    # Twelve Data
    if body.get("status") == "error":
        return str(body.get("message", "provider error"))

    # Alpha Vantage
    for key in ("Error Message", "Information"):
        if key in body:
            return str(body[key])[:300]

    # Alpha Vantage soft rate limit — data is absent, so treat it as an error
    if "Note" in body and len(body) == 1:
        return str(body["Note"])[:300]

    return None


def pause(seconds: int, reason: str = "") -> None:
    if seconds <= 0:
        return
    if reason:
        logger.debug("waiting %ss (%s)", seconds, reason)
    time.sleep(seconds)
