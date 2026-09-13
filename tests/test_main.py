import sys
from datetime import UTC, datetime

import pytest

import fleet_intelligence.main as main_module
from fleet_intelligence.config import DatabaseSettings, PipelineSettings
from fleet_intelligence.models import ExtractResult


class Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def settings() -> PipelineSettings:
    return PipelineSettings(
        system_information_url="https://example.test/system",
        station_information_url="https://example.test/stations",
        station_status_url="https://example.test/station-status",
        free_bike_status_url="https://example.test/vehicles",
        detail_retention_hours=24,
        aggregate_retention_days=35,
        pipeline_run_retention_days=90,
        rejected_retention_days=14,
        feed_stale_minutes=10,
        volume_drop_threshold=0.5,
        volume_drop_policy="WARNING",
        bbox=None,
        grid_size_degrees=0.01,
        rebalance_high_threshold=10,
        rebalance_medium_threshold=5,
    )


def feed_results() -> dict[str, ExtractResult]:
    timestamp = int(datetime.now(UTC).timestamp())
    return {
        "system_information": ExtractResult(
            {
                "last_updated": timestamp,
                "data": {
                    "system_id": "lime_seattle",
                    "name": "Lime Seattle",
                    "language": "en",
                    "timezone": "America/Los_Angeles",
                },
            },
            200,
            5,
        ),
        "station_information": ExtractResult(
            {
                "last_updated": timestamp,
                "data": {
                    "stations": [
                        {"station_id": "seattle", "name": "Seattle", "lat": 47.6, "lon": -122.3}
                    ]
                },
            },
            200,
            5,
        ),
        "station_status": ExtractResult(
            {
                "last_updated": timestamp,
                "data": {
                    "stations": [
                        {
                            "station_id": "seattle",
                            "num_vehicles_available": 1,
                            "num_docks_available": 2,
                            "is_installed": True,
                            "is_renting": True,
                            "is_returning": True,
                            "last_reported": timestamp,
                        }
                    ]
                },
            },
            200,
            5,
        ),
        "free_bike_status": ExtractResult(
            {
                "last_updated": timestamp,
                "data": {
                    "bikes": [
                        {
                            "bike_id": "a",
                            "lat": 47.6,
                            "lon": -122.3,
                            "is_reserved": False,
                            "is_disabled": False,
                        }
                    ]
                },
            },
            200,
            5,
        ),
    }


def patch_happy_path(
    monkeypatch: pytest.MonkeyPatch, quality: str = "SUCCESS"
) -> tuple[Connection, dict[str, object]]:
    connection = Connection()
    captured: dict[str, object] = {}
    monkeypatch.setattr(main_module, "load_pipeline_settings", settings)
    monkeypatch.setattr(
        main_module,
        "load_database_settings",
        lambda: DatabaseSettings("db", 5432, "postgres", "fleet", "top-secret"),
    )
    monkeypatch.setattr(main_module, "connect", lambda _: connection)
    monkeypatch.setattr(main_module, "get_recent_baseline_count", lambda _: 100)
    monkeypatch.setattr(
        main_module,
        "fetch_gbfs_feeds",
        lambda _: feed_results(),
    )
    monkeypatch.setattr(
        main_module,
        "calculate_quality_status",
        lambda **_: (quality, []),
    )
    monkeypatch.setattr(
        main_module,
        "load_snapshot",
        lambda *args, **kwargs: captured.update(load=(args, kwargs)),
    )
    monkeypatch.setattr(
        main_module,
        "upsert_pipeline_run",
        lambda _conn, run: captured.update(final=run),
    )
    monkeypatch.setattr(main_module, "_elapsed_ms", lambda _: 123)
    return connection, captured


@pytest.mark.parametrize(("quality", "expected"), [("SUCCESS", 0), ("WARNING", 0), ("FAILED", 1)])
def test_run_exit_and_retention_contract(
    monkeypatch: pytest.MonkeyPatch, quality: str, expected: int
) -> None:
    connection, captured = patch_happy_path(monkeypatch, quality)
    assert main_module.run() == expected
    load_kwargs = captured["load"][1]  # type: ignore[index]
    assert load_kwargs["detail_retention_hours"] == 24
    assert load_kwargs["aggregate_retention_days"] == 35
    assert load_kwargs["system_information"].system_id == "lime_seattle"
    assert len(load_kwargs["station_information"]) == 1
    assert len(load_kwargs["station_status"]) == 1
    assert len(load_kwargs["feed_metrics"]) == 4
    assert captured["final"].pipeline_duration_ms == 123  # type: ignore[union-attr]
    assert connection.closed is True


def test_failure_is_sanitized_logged_and_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    connection, _ = patch_happy_path(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        main_module,
        "fetch_gbfs_feeds",
        lambda _: (_ for _ in ()).throw(RuntimeError("top-secret connection failed")),
    )
    monkeypatch.setattr(
        main_module,
        "log_failed_run",
        lambda _conn, run: captured.update(run=run),
    )
    assert main_module.run() == 1
    assert "top-secret" not in captured["run"].error_message  # type: ignore[union-attr]
    assert connection.closed is True


def test_secondary_failure_logging_does_not_hide_original(monkeypatch: pytest.MonkeyPatch) -> None:
    connection, _ = patch_happy_path(monkeypatch)
    monkeypatch.setattr(main_module, "load_snapshot", lambda *_args, **_kwargs: 1 / 0)
    monkeypatch.setattr(main_module, "log_failed_run", lambda *_: 1 / 0)
    assert main_module.run() == 1
    assert connection.closed is True


def test_failure_before_connection_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module,
        "load_pipeline_settings",
        lambda: (_ for _ in ()).throw(ValueError("bad config")),
    )
    assert main_module.run() == 1


def test_dry_run_never_loads_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "load_pipeline_settings", settings)
    monkeypatch.setattr(
        main_module,
        "fetch_gbfs_feeds",
        lambda _: feed_results(),
    )
    monkeypatch.setattr(main_module, "calculate_quality_status", lambda **_: ("SUCCESS", []))
    monkeypatch.setattr(
        main_module,
        "load_database_settings",
        lambda: (_ for _ in ()).throw(AssertionError("database config loaded")),
    )
    assert main_module.dry_run() == 0


def test_dry_run_failure_and_cli_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module,
        "load_pipeline_settings",
        lambda: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert main_module.dry_run() == 1

    monkeypatch.setattr(sys, "argv", ["fleet-ingest", "--dry-run"])
    monkeypatch.setattr(main_module, "dry_run", lambda: 7)
    assert main_module.main() == 7


def test_error_length_is_bounded() -> None:
    assert len(main_module._sanitize_error(RuntimeError("x" * 3_000), [])) == 2_000


def test_related_feed_quality_can_escalate_and_explain() -> None:
    status, reasons = main_module._merge_quality(
        "SUCCESS", [], "WARNING", ["station_status: stale"]
    )
    assert status == "WARNING"
    assert reasons == ["station_status: stale"]

    status, reasons = main_module._assess_related_feeds("SUCCESS", [], 1, 1, 1, 0, 0, 0)
    assert status == "FAILED"
    assert "zero valid" in "; ".join(reasons)
    assert "rejected 1" in "; ".join(reasons)
