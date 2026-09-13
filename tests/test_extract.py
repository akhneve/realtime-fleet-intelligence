from collections.abc import Iterator

import pytest
import requests

import fleet_intelligence.extract as extract_module
from fleet_intelligence.extract import USER_AGENT, fetch_gbfs, fetch_gbfs_feeds


class FakeResponse:
    def __init__(self, status: int = 200, payload: object = None, json_error: bool = False):
        self.status_code = status
        self.payload = payload if payload is not None else {"data": {"bikes": []}}
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        if self.json_error:
            raise ValueError("bad json")
        return self.payload


class FakeSession:
    def __init__(self, outcomes: list[object]):
        self.outcomes: Iterator[object] = iter(outcomes)
        self.headers: dict[str, str] = {}
        self.calls = 0
        self.closed = False

    def get(self, _url: str, timeout: tuple[float, float]) -> FakeResponse:
        assert timeout == (5.0, 30.0)
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    def close(self) -> None:
        self.closed = True


def test_success_uses_explicit_headers_and_caller_session() -> None:
    session = FakeSession([FakeResponse(payload={"data": {"bikes": []}})])
    result = fetch_gbfs("https://example.test/feed", session=session)  # type: ignore[arg-type]
    assert result.http_status == 200
    assert session.headers["User-Agent"] == USER_AGENT
    assert session.closed is False


@pytest.mark.parametrize(
    "failure",
    [
        requests.Timeout("timeout"),
        requests.ConnectionError("connection"),
        FakeResponse(429),
        FakeResponse(503),
    ],
)
def test_transient_failures_are_retried(monkeypatch: pytest.MonkeyPatch, failure: object) -> None:
    monkeypatch.setattr(extract_module.time, "sleep", lambda _: None)
    session = FakeSession([failure, FakeResponse()])
    assert fetch_gbfs("https://example.test/feed", session=session).http_status == 200  # type: ignore[arg-type]
    assert session.calls == 2


def test_retry_exhaustion_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_module.time, "sleep", lambda _: None)
    session = FakeSession([requests.Timeout("one"), requests.Timeout("two")])
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        fetch_gbfs("https://example.test/feed", session=session, retries=1)  # type: ignore[arg-type]


def test_permanent_4xx_is_not_retried() -> None:
    session = FakeSession([FakeResponse(404), FakeResponse()])
    with pytest.raises(RuntimeError, match="non-retryable HTTP 404"):
        fetch_gbfs("https://example.test/feed", session=session)  # type: ignore[arg-type]
    assert session.calls == 1


@pytest.mark.parametrize(
    "response,message",
    [
        (FakeResponse(json_error=True), "valid JSON"),
        (FakeResponse(payload=[]), "not a JSON object"),
        (FakeResponse(payload={}), "not a JSON object"),
    ],
)
def test_invalid_response_fails_without_retry(response: FakeResponse, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        fetch_gbfs("https://example.test/feed", session=FakeSession([response]))  # type: ignore[arg-type]


def test_owned_session_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([FakeResponse()])
    monkeypatch.setattr(extract_module.requests, "Session", lambda: session)
    fetch_gbfs("https://example.test/feed")
    assert session.closed is True


def test_negative_retries_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        fetch_gbfs("https://example.test/feed", retries=-1)


def test_all_feeds_share_and_close_one_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([FakeResponse() for _ in range(4)])
    monkeypatch.setattr(extract_module.requests, "Session", lambda: session)
    results = fetch_gbfs_feeds(
        {
            "system_information": "https://example.test/system",
            "station_information": "https://example.test/stations",
            "station_status": "https://example.test/status",
            "free_bike_status": "https://example.test/vehicles",
        }
    )
    assert list(results) == [
        "system_information",
        "station_information",
        "station_status",
        "free_bike_status",
    ]
    assert session.calls == 4
    assert session.closed is True


def test_multi_feed_failure_names_feed_and_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([FakeResponse(404)])
    monkeypatch.setattr(extract_module.requests, "Session", lambda: session)
    with pytest.raises(RuntimeError, match="station_status extraction failed"):
        fetch_gbfs_feeds({"station_status": "https://example.test/status"})
    assert session.closed is True
