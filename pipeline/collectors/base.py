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
    GET a JSON endpoint, log the call, and return the parsed body.

    Raises ApiError on any failure, after the call has been logged.
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
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if status == 429:
            error = "rate limit exceeded"
            raise ApiError(f"{source} rate limit hit on {endpoint}")

        response.raise_for_status()
        body = response.json()

        # Providers often return HTTP 200 with an error inside the body.
        provider_error = _provider_error(body)
        if provider_error:
            error = provider_error
            raise ApiError(f"{source} {endpoint}: {provider_error}")

        return body

    except ApiError:
        raise

    except requests.exceptions.Timeout as exc:
        error = f"timeout after {config.HTTP_TIMEOUT}s"
        raise ApiError(f"{source} {endpoint}: {error}") from exc

    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise ApiError(f"{source} {endpoint}: {error}") from exc

    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        db.log_api_call(
            source=source,
            symbol=symbol,
            endpoint=endpoint,
            http_status=status,
            response_ms=elapsed_ms,
            error=error,
            run_id=run_id,
        )


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
