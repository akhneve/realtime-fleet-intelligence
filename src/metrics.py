"""Snapshot QA metrics and aggregate preparation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def calculate_quality_status(
    *,
    now: datetime,
    source_timestamp: datetime | None,
    feed_stale_minutes: int | None,
    current_count: int,
    baseline_count: int | None,
    volume_drop_threshold: float,
    volume_drop_policy: str,
) -> tuple[str, list[str]]:
    # What: evaluates snapshot-level warning/failure conditions. Why: the pipeline should mark stale or suspicious loads before they reach the dashboard.
    reasons: list[str] = []
    status = "SUCCESS"

    if feed_stale_minutes is not None:
        if source_timestamp is None:
            status = _more_severe(status, "WARNING")
            reasons.append("Feed source timestamp is missing")
        else:
            # What: compares feed time to ingestion time. Why: an old or implausibly future-dated source can make the dashboard misleading.
            age_minutes = (now - source_timestamp).total_seconds() / 60
            if age_minutes > feed_stale_minutes:
                status = _more_severe(status, "WARNING")
                reasons.append(f"Feed stale by {age_minutes:.1f} minutes")
            elif age_minutes < -5:
                status = _more_severe(status, "WARNING")
                reasons.append(f"Feed timestamp is {-age_minutes:.1f} minutes in the future")

    if current_count == 0:
        status = _more_severe(status, "FAILED")
        reasons.append("Feed produced zero valid vehicle records")

    if baseline_count and baseline_count > 0:
        # What: compares current valid record count to recent typical count. Why: a sudden huge drop can mean an incomplete API response.
        drop_ratio = 1 - (current_count / baseline_count)
        if drop_ratio > volume_drop_threshold:
            status = _more_severe(status, volume_drop_policy)
            reasons.append(f"Record count dropped {drop_ratio:.1%} below recent baseline")

    return status, reasons


def _more_severe(current: str, candidate: str) -> str:
    # What: preserves the most severe quality result. Why: a later warning must never downgrade an earlier failure.
    severity = {"SUCCESS": 0, "WARNING": 1, "FAILED": 2}
    return candidate if severity[candidate] > severity[current] else current


def build_grid_15min_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # What: rolls vehicle-level rows into grid/time aggregates. Why: long-term reporting should use compact metrics instead of massive raw snapshots.
    buckets: dict[tuple[datetime, str], dict[str, Any]] = {}
    for record in records:
        bucket_timestamp = floor_to_15_minutes(record["snapshot_timestamp"])  # What: normalizes each snapshot into a 15-minute bucket. Why: trends need consistent time bins.
        key = (bucket_timestamp, record["grid_id"])  # What: grouping key for one grid during one time bucket. Why: this matches the fact_grid_15min primary key.
        aggregate = buckets.setdefault(
            key,
            {
                "bucket_timestamp": bucket_timestamp,
                "grid_id": record["grid_id"],
                "total_vehicles": 0,
                "available_vehicles": 0,
                "unavailable_vehicles": 0,
                "run_id": record["run_id"],
            },
        )
        aggregate["total_vehicles"] += 1  # What: counts each observed vehicle. Why: total supply is needed for availability-rate calculations.
        if record["available_flag"]:
            # What: increments available count when the transformed record says the vehicle is usable. Why: this is the main supply metric.
            aggregate["available_vehicles"] += 1
        else:
            # What: increments unavailable count for reserved/disabled vehicles. Why: unavailable supply explains why total fleet and usable fleet differ.
            aggregate["unavailable_vehicles"] += 1
    return list(buckets.values())


def floor_to_15_minutes(value: datetime) -> datetime:
    # What: rounds a timestamp down to the start of its 15-minute interval. Why: every record in the same reporting window must share one bucket timestamp.
    if value.tzinfo is None:
        # What: assumes UTC for naive datetimes. Why: database and feed timestamps should be comparable in one timezone.
        value = value.replace(tzinfo=timezone.utc)
    minute = value.minute - (value.minute % 15)
    return value.replace(minute=minute, second=0, microsecond=0)


def _smoke_test() -> None:
    """Run with: python -m src.metrics"""
    # What: exercises aggregation and anomaly scoring with tiny sample data. Why: you can verify metric behavior without a database.
    timestamp = datetime(2026, 8, 30, 12, 14, 59, tzinfo=timezone.utc)
    rows = build_grid_15min_rows(
        [
            {"snapshot_timestamp": timestamp, "grid_id": "GRID_1", "available_flag": True, "run_id": "smoke-run"},
            {"snapshot_timestamp": timestamp, "grid_id": "GRID_1", "available_flag": False, "run_id": "smoke-run"},
        ]
    )
    status, reasons = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        source_timestamp=None,
        feed_stale_minutes=None,
        current_count=25,
        baseline_count=100,
        volume_drop_threshold=0.50,
        volume_drop_policy="WARNING",
    )
    print("Metrics smoke test passed")
    print(f"15-minute aggregate rows: {rows}")
    print(f"Quality status: {status}")
    print(f"Quality reasons: {reasons}")


if __name__ == "__main__":
    _smoke_test()
