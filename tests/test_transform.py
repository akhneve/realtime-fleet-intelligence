from datetime import UTC, datetime

import pytest

from fleet_intelligence.metrics import (
    build_grid_15min_rows,
    calculate_quality_status,
    floor_to_15_minutes,
)
from fleet_intelligence.models import (
    NormalizedStationInformation,
    NormalizedStationStatus,
    NormalizedSystemInformation,
    NormalizedVehicle,
)
from fleet_intelligence.transform import (
    assign_grid_id,
    derive_available_flag,
    transform_records,
    transform_station_information,
    transform_station_status,
    transform_system_information,
)


def normalized(**overrides: object) -> NormalizedVehicle:
    values: dict[str, object] = {
        "vehicle_id": "a",
        "vehicle_type_id": "scooter",
        "latitude": 47.61,
        "longitude": -122.33,
        "is_reserved": False,
        "is_disabled": False,
    }
    values.update(overrides)
    return NormalizedVehicle(**values)  # type: ignore[arg-type]


def test_transform_adds_metadata_and_availability() -> None:
    timestamp = datetime(2026, 8, 30, 12, 3, tzinfo=UTC)
    records = transform_records(
        [normalized(is_disabled=True)],
        run_id="00000000-0000-0000-0000-000000000001",
        source_timestamp=timestamp,
        ingestion_timestamp=timestamp,
        grid_size_degrees=0.01,
        quality_flag="SUCCESS",
    )
    assert records[0].available_flag is False
    assert records[0].snapshot_timestamp == timestamp
    assert records[0].grid_id == assign_grid_id(47.61, -122.33)
    assert derive_available_flag(False, False) is True


def test_ingestion_time_is_snapshot_fallback() -> None:
    timestamp = datetime(2026, 8, 30, 12, 3, tzinfo=UTC)
    record = transform_records(
        [normalized()],
        run_id="00000000-0000-0000-0000-000000000001",
        source_timestamp=None,
        ingestion_timestamp=timestamp,
        grid_size_degrees=0.01,
        quality_flag="WARNING",
    )[0]
    assert record.snapshot_timestamp == timestamp


def test_grid_assignment_validation_and_bucketing() -> None:
    assert assign_grid_id(47.61, -122.33).startswith("GRID_")
    with pytest.raises(ValueError, match="positive"):
        assign_grid_id(47.61, -122.33, 0)
    naive = datetime(2026, 8, 30, 12, 14, 59)
    assert floor_to_15_minutes(naive) == datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_grid_aggregate_counts_availability() -> None:
    timestamp = datetime(2026, 8, 30, 12, 3, tzinfo=UTC)
    records = transform_records(
        [normalized(vehicle_id="a"), normalized(vehicle_id="b", is_disabled=True)],
        run_id="00000000-0000-0000-0000-000000000001",
        source_timestamp=timestamp,
        ingestion_timestamp=timestamp,
        grid_size_degrees=0.01,
        quality_flag="SUCCESS",
    )
    aggregate = build_grid_15min_rows(records)[0]
    assert aggregate.total_vehicles == 2
    assert aggregate.available_vehicles == 1
    assert aggregate.unavailable_vehicles == 1


@pytest.mark.parametrize(
    ("source_timestamp", "current", "baseline", "policy", "expected"),
    [
        (None, 10, None, "WARNING", "WARNING"),
        (datetime(2026, 8, 30, 11, 0, tzinfo=UTC), 10, None, "WARNING", "WARNING"),
        (datetime(2026, 8, 30, 12, 10, tzinfo=UTC), 10, None, "WARNING", "WARNING"),
        (datetime(2026, 8, 30, 12, 0, tzinfo=UTC), 0, None, "WARNING", "FAILED"),
        (datetime(2026, 8, 30, 12, 0, tzinfo=UTC), 25, 100, "FAILED", "FAILED"),
    ],
)
def test_quality_conditions(
    source_timestamp: datetime | None,
    current: int,
    baseline: int | None,
    policy: str,
    expected: str,
) -> None:
    status, reasons = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        source_timestamp=source_timestamp,
        feed_stale_minutes=10,
        current_count=current,
        baseline_count=baseline,
        volume_drop_threshold=0.5,
        volume_drop_policy=policy,  # type: ignore[arg-type]
    )
    assert status == expected
    assert reasons


def test_clean_quality_result_has_no_reasons() -> None:
    status, reasons = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        source_timestamp=datetime(2026, 8, 30, 11, 55, tzinfo=UTC),
        feed_stale_minutes=10,
        current_count=100,
        baseline_count=100,
        volume_drop_threshold=0.5,
        volume_drop_policy="WARNING",
    )
    assert status == "SUCCESS"
    assert reasons == []


def test_related_feed_transforms_add_lineage_and_snapshot_time() -> None:
    timestamp = datetime(2026, 8, 30, 12, 3, tzinfo=UTC)
    run_id = "00000000-0000-0000-0000-000000000001"
    system = transform_system_information(
        NormalizedSystemInformation(
            "lime_seattle", "Lime Seattle", "en", "America/Los_Angeles", None, "Lime"
        ),
        run_id=run_id,
        source_timestamp=timestamp,
        ingestion_timestamp=timestamp,
    )
    stations = transform_station_information(
        [NormalizedStationInformation("seattle", "Seattle", None, 47.61, -122.33, None, None)],
        run_id=run_id,
        source_timestamp=timestamp,
        ingestion_timestamp=timestamp,
    )
    statuses = transform_station_status(
        [NormalizedStationStatus("seattle", 10, 20, True, True, True, timestamp)],
        run_id=run_id,
        source_timestamp=None,
        ingestion_timestamp=timestamp,
        quality_flag="SUCCESS",
    )
    assert system.run_id == run_id
    assert stations[0].latitude == 47.61
    assert statuses[0].snapshot_timestamp == timestamp
    assert statuses[0].quality_flag == "SUCCESS"
