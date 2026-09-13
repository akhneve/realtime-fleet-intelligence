"""HTTP boundary for GBFS extraction."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import cast

import requests

from .models import ExtractResult, FeedName, JsonObject

USER_AGENT = (
    "realtime-fleet-intelligence/1.1 (+https://github.com/akhneve/realtime-fleet-intelligence)"
)
DEFAULT_TIMEOUT = (5.0, 30.0)


class _RetryableStatusError(RuntimeError):
    pass


def fetch_gbfs_feeds(
    urls: Mapping[FeedName, str],
    *,
    retries: int = 2,
    backoff_seconds: float = 1.0,
) -> dict[FeedName, ExtractResult]:
    """Fetch a related set of GBFS documents through one reusable HTTP session."""

    client = requests.Session()
    results: dict[FeedName, ExtractResult] = {}
    try:
        for feed_name, url in urls.items():
            try:
                results[feed_name] = fetch_gbfs(
                    url,
                    session=client,
                    retries=retries,
                    backoff_seconds=backoff_seconds,
                )
            except RuntimeError as exc:
                raise RuntimeError(f"{feed_name} extraction failed: {exc}") from exc
        return results
    finally:
        client.close()


def fetch_gbfs(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retries: int = 2,
    backoff_seconds: float = 1.0,
) -> ExtractResult:
    """Fetch one GBFS document, retrying only failures that can reasonably recover."""

    if retries < 0:
        raise ValueError("retries must be non-negative")

    owned_session = session is None
    client = session or requests.Session()
    client.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
    last_error: Exception | None = None

    try:
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                response = client.get(url, timeout=timeout)
                latency_ms = int((time.perf_counter() - started) * 1000)
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableStatusError(f"HTTP {response.status_code}")
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    raise RuntimeError(
                        f"GBFS request failed with non-retryable HTTP {response.status_code}"
                    ) from exc
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("GBFS response was not valid JSON") from exc
                if not isinstance(payload, dict) or not payload:
                    raise RuntimeError("GBFS response was empty or not a JSON object")
                return ExtractResult(cast(JsonObject, payload), response.status_code, latency_ms)
            except (requests.Timeout, requests.ConnectionError, _RetryableStatusError) as exc:
                last_error = exc
                if attempt == retries:
                    break
                time.sleep(backoff_seconds * (2**attempt))
    finally:
        if owned_session:
            client.close()

    raise RuntimeError(
        f"GBFS request failed after {retries + 1} attempts: {last_error}"
    ) from last_error
