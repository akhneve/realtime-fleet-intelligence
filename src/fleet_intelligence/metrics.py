"""Snapshot-level quality checks and reporting aggregates."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .models import GridAggregate, QualityStatus, VehicleSnapshot, VolumeDropPolicy


def calculate_quality_status(
    *,
    now: datetime,
    source_timestamp: datetime | None,
    feed_stale_minutes: int,
    current_count: int,
    baseline_count: int | None,
    volume_drop_threshold: float,
    volume_drop_policy: VolumeDropPolicy,
) -> tuple[QualityStatus, list[str]]:
    reasons: list[str] = []
    status: QualityStatus = "SUCCESS"

    if source_timestamp is None:
        status = _more_severe(status, "WARNING")
        reasons.append("Feed source timestamp is missing or invalid")
    else:
        age_minutes = (now - source_timestamp).total_seconds() / 60
        if age_minutes > feed_stale_minutes:
            status = _more_severe(status, "WARNING")
            reasons.append(f"Feed stale by {age_minutes:.1f} minutes")
        elif age_minutes < -5:
            status = _more_severe(status, "WARNING")
            reasons.append(f"Feed timestamp is {-age_minutes:.1f} minutes in the future")

    if current_count == 0:
        status = "FAILED"
        reasons.append("Feed produced zero valid vehicle records")

    if baseline_count is not None and baseline_count > 0:
        drop_ratio = 1 - (current_count / baseline_count)
        if drop_ratio > volume_drop_threshold:
            status = _more_severe(status, volume_drop_policy)
            reasons.append(f"Record count dropped {drop_ratio:.1%} below recent baseline")

    return status, reasons


def build_grid_15min_rows(records: list[VehicleSnapshot]) -> list[GridAggregate]:
    buckets: dict[tuple[datetime, str], GridAggregate] = {}
    for record in records:
        bucket_timestamp = floor_to_15_minutes(record.snapshot_timestamp)
        key = (bucket_timestamp, record.grid_id)
        aggregate = buckets.get(key)
        if aggregate is None:
            aggregate = GridAggregate(bucket_timestamp, record.grid_id, 0, 0, 0, record.run_id)
        buckets[key] = replace(
            aggregate,
            total_vehicles=aggregate.total_vehicles + 1,
            available_vehicles=aggregate.available_vehicles + int(record.available_flag),
            unavailable_vehicles=aggregate.unavailable_vehicles + int(not record.available_flag),
        )
    return list(buckets.values())


def floor_to_15_minutes(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    minute = value.minute - (value.minute % 15)
    return value.replace(minute=minute, second=0, microsecond=0)


def _more_severe(current: QualityStatus, candidate: QualityStatus) -> QualityStatus:
    severity = {"SUCCESS": 0, "WARNING": 1, "FAILED": 2}
    return candidate if severity[candidate] > severity[current] else current
