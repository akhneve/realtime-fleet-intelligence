from __future__ import annotations

import sys

import pytest

import fleet_intelligence.diagnostics as diagnostics
from fleet_intelligence.models import ExtractResult


class Cursor:
    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def execute(self, *_: object) -> None:
        pass

    def fetchall(self) -> list[tuple[str]]:
        return []


class Connection:
    def __init__(self) -> None:
        self.closed = False

    def cursor(self) -> Cursor:
        return Cursor()

    def close(self) -> None:
        self.closed = True


def test_database_diagnostic_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Connection()
    monkeypatch.setattr(diagnostics, "load_database_settings", lambda: object())
    monkeypatch.setattr(diagnostics, "connect", lambda _: connection)
    monkeypatch.setattr(diagnostics, "get_recent_baseline_count", lambda _: 10)
    diagnostics.check_database()
    assert connection.closed is True


def test_live_diagnostic_requires_accepted_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "load_pipeline_settings",
        lambda: type(
            "Settings",
            (),
            {
                "feed_urls": {
                    "system_information": "https://test/system",
                    "station_information": "https://test/stations",
                    "station_status": "https://test/status",
                    "free_bike_status": "https://test/vehicles",
                },
                "bbox": None,
            },
        )(),
    )
    monkeypatch.setattr(
        diagnostics,
        "fetch_gbfs_feeds",
        lambda *_args, **_kwargs: {
            "system_information": ExtractResult(
                {
                    "data": {
                        "system_id": "lime_seattle",
                        "name": "Lime Seattle",
                        "language": "en",
                        "timezone": "America/Los_Angeles",
                    }
                },
                200,
                1,
            ),
            "station_information": ExtractResult(
                {
                    "data": {
                        "stations": [
                            {
                                "station_id": "seattle",
                                "name": "Seattle",
                                "lat": 47.6,
                                "lon": -122.3,
                            }
                        ]
                    }
                },
                200,
                1,
            ),
            "station_status": ExtractResult(
                {
                    "data": {
                        "stations": [
                            {
                                "station_id": "seattle",
                                "num_vehicles_available": 0,
                                "num_docks_available": 0,
                                "is_installed": True,
                                "is_renting": True,
                                "is_returning": True,
                            }
                        ]
                    }
                },
                200,
                1,
            ),
            "free_bike_status": ExtractResult({"data": {"bikes": []}}, 200, 1),
        },
    )
    with pytest.raises(RuntimeError, match="zero valid"):
        diagnostics.check_live_feed()


def test_diagnostics_cli_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["fleet-diagnostics", "--live", "--database"])
    monkeypatch.setattr(diagnostics, "check_live_feed", lambda: None)
    monkeypatch.setattr(diagnostics, "check_database", lambda: None)
    assert diagnostics.main() == 0

    monkeypatch.setattr(diagnostics, "check_live_feed", lambda: 1 / 0)
    assert diagnostics.main() == 1


def test_diagnostics_cli_requires_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["fleet-diagnostics"])
    with pytest.raises(SystemExit):
        diagnostics.main()
