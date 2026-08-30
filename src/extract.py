"""GBFS extraction with timeout, retry, and latency measurement."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

try:
    # What: imports requests when installed. Why: keeping this optional makes the module show a clear setup error in lightweight environments.
    import requests
except ModuleNotFoundError:
    requests = None


@dataclass(frozen=True)
class ExtractResult:
    # What: return object for the API payload and request metadata. Why: later pipeline steps need both data and observability metrics.
    payload: dict[str, Any]
    http_status: int
    latency_ms: int


def fetch_gbfs(url: str, timeout_seconds: int = 30, retries: int = 2, backoff_seconds: float = 1.0) -> ExtractResult:
    # What: downloads and parses the GBFS JSON feed. Why: extraction is isolated so retry/timeout behavior is testable and reusable.
    if requests is None:
        raise RuntimeError("requests is required for GBFS extraction. Install dependencies with: pip install -r requirements.txt")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        # What: retry transient network/HTTP/JSON failures. Why: scheduled jobs should tolerate short-lived API hiccups.
        started = time.perf_counter()
        try:
            response = requests.get(url, timeout=timeout_seconds)
            latency_ms = int((time.perf_counter() - started) * 1000)  # What: captures API latency. Why: pipeline health needs source responsiveness.
            response.raise_for_status()  # What: converts non-2xx responses into exceptions. Why: bad API responses should fail the run.
            payload = response.json()  # What: parses JSON into Python structures. Why: validation expects a dictionary payload.
            if not isinstance(payload, dict) or not payload:
                # What: rejects empty/non-object responses. Why: downstream code assumes GBFS has top-level metadata and data sections.
                raise ValueError("GBFS response was empty or not a JSON object")
            return ExtractResult(payload=payload, http_status=response.status_code, latency_ms=latency_ms)
        except (requests.RequestException, ValueError) as exc:
            # What: remembers the most recent failure. Why: the final error should explain what actually went wrong.
            last_error = exc
            if attempt < retries:
                # What: waits longer after each failed attempt. Why: exponential backoff reduces pressure on a temporarily unhealthy feed.
                time.sleep(backoff_seconds * (2**attempt))
    raise RuntimeError(f"Failed to fetch GBFS feed after {retries + 1} attempts: {last_error}") from last_error


def _smoke_test() -> None:
    """Run with: python -m src.extract"""
    # What: calls the live public feed and prints basic shape/volume. Why: this is the fastest manual check that extraction still works.
    url = "https://data.lime.bike/api/partners/v1/gbfs/seattle/free_bike_status.json"
    result = fetch_gbfs(url, timeout_seconds=30, retries=1)
    bikes = result.payload.get("data", {}).get("bikes", [])
    print("Extract smoke test passed")
    print(f"HTTP status: {result.http_status}")
    print(f"Latency ms: {result.latency_ms}")
    print(f"Top-level keys: {sorted(result.payload.keys())}")
    print(f"Vehicle records: {len(bikes)}")


if __name__ == "__main__":
    _smoke_test()
