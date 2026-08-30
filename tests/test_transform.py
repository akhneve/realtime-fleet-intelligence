from datetime import datetime, timezone

from src.metrics import build_grid_15min_rows, calculate_quality_status, floor_to_15_minutes
from src.transform import assign_grid_id, derive_available_flag, transform_records


def test_available_flag_calculated_from_reserved_and_disabled():
    assert derive_available_flag(False, False) is True
    assert derive_available_flag(True, False) is False
    assert derive_available_flag(False, True) is False
    assert derive_available_flag(None, None) is False


def test_grid_assignment_is_deterministic():
    first = assign_grid_id(47.61, -122.33, 0.01)
    second = assign_grid_id(47.61, -122.33, 0.01)

    assert first == second
    assert first.startswith("GRID_")


def test_transform_adds_metadata_and_normalized_fields():
    ts = datetime(2026, 8, 30, 12, 3, tzinfo=timezone.utc)
    records = [{"vehicle_id": "a", "latitude": 47.61, "longitude": -122.33, "is_reserved": False, "is_disabled": True}]

    transformed = transform_records(records, "run-1", ts, ts, "test_feed", 0.01, "SUCCESS")

    assert transformed[0]["available_flag"] is False
    assert transformed[0]["snapshot_timestamp"] == ts
    assert transformed[0]["source_feed"] == "test_feed"
    assert transformed[0]["quality_flag"] == "SUCCESS"


def test_floor_to_15_minutes_normalizes_timestamp():
    ts = datetime(2026, 8, 30, 12, 14, 59, tzinfo=timezone.utc)

    assert floor_to_15_minutes(ts) == datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def test_grid_aggregate_counts_availability():
    ts = datetime(2026, 8, 30, 12, 3, tzinfo=timezone.utc)
    rows = build_grid_15min_rows(
        [
            {"snapshot_timestamp": ts, "grid_id": "GRID_1", "available_flag": True, "run_id": "run-1"},
            {"snapshot_timestamp": ts, "grid_id": "GRID_1", "available_flag": False, "run_id": "run-1"},
        ]
    )

    assert rows == [
        {
            "bucket_timestamp": datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
            "grid_id": "GRID_1",
            "total_vehicles": 2,
            "available_vehicles": 1,
            "unavailable_vehicles": 1,
            "run_id": "run-1",
        }
    ]


def test_volume_drop_can_flag_failed_policy():
    status, reasons = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        source_timestamp=None,
        feed_stale_minutes=None,
        current_count=25,
        baseline_count=100,
        volume_drop_threshold=0.50,
        volume_drop_policy="FAILED",
    )

    assert status == "FAILED"
    assert reasons


def test_zero_valid_records_fail_without_a_baseline():
    status, reasons = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        source_timestamp=None,
        feed_stale_minutes=None,
        current_count=0,
        baseline_count=None,
        volume_drop_threshold=0.50,
        volume_drop_policy="WARNING",
    )

    assert status == "FAILED"
    assert "zero valid" in reasons[0].lower()


def test_warning_policy_cannot_downgrade_zero_record_failure():
    status, _ = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        source_timestamp=None,
        feed_stale_minutes=None,
        current_count=0,
        baseline_count=100,
        volume_drop_threshold=0.50,
        volume_drop_policy="WARNING",
    )

    assert status == "FAILED"


def test_missing_timestamp_warns_when_freshness_check_is_enabled():
    status, reasons = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        source_timestamp=None,
        feed_stale_minutes=10,
        current_count=10,
        baseline_count=None,
        volume_drop_threshold=0.50,
        volume_drop_policy="WARNING",
    )

    assert status == "WARNING"
    assert "timestamp" in reasons[0].lower()
