"""Deterministic transformations for accepted GBFS records."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


def transform_records(
    records: list[dict[str, Any]],
    run_id: str,
    source_timestamp: datetime | None,
    ingestion_timestamp: datetime,
    source_feed: str,
    grid_size_degrees: float,
    quality_flag: str,
) -> list[dict[str, Any]]:
    # What: enriches accepted validation rows with derived fields and ingestion metadata. Why: database tables and Power BI need business-ready rows.
    snapshot_timestamp = source_timestamp or ingestion_timestamp
    return [
        {
            **record,
            "available_flag": derive_available_flag(record.get("is_reserved"), record.get("is_disabled")),  # What: availability boolean. Why: BI should not duplicate core operational logic.
            "grid_id": assign_grid_id(record["latitude"], record["longitude"], grid_size_degrees),  # What: geographic bucket. Why: grid reporting is cheaper and clearer than raw point-only analysis.
            "run_id": run_id,  # What: ingestion lineage ID. Why: every loaded row should be traceable to one pipeline run.
            "snapshot_timestamp": snapshot_timestamp,  # What: event timestamp for the snapshot. Why: historical facts need a consistent time axis.
            "source_timestamp": source_timestamp,  # What: raw feed timestamp when available. Why: freshness checks depend on source time.
            "ingestion_timestamp": ingestion_timestamp,  # What: processing timestamp. Why: observability needs to know when this system saw the data.
            "source_feed": source_feed,  # What: source label. Why: lineage stays clear if additional feeds are added later.
            "quality_flag": quality_flag,  # What: QA status copied onto rows. Why: analysts can filter warning/failed-quality snapshots.
        }
        for record in records
    ]


def derive_available_flag(is_reserved: bool | None, is_disabled: bool | None) -> bool:
    # What: applies GBFS availability logic. Why: a vehicle is usable only when it is neither reserved nor disabled.
    return is_reserved is False and is_disabled is False


def assign_grid_id(latitude: float, longitude: float, grid_size_degrees: float = 0.01) -> str:
    # What: converts lat/lon into a deterministic rectangular grid ID. Why: MVP aggregation needs a simple location bucket without adding geospatial dependencies.
    if grid_size_degrees <= 0:
        raise ValueError("grid_size_degrees must be positive")
    lat_bucket = math.floor((latitude + 90) / grid_size_degrees)  # What: shifts latitude to a non-negative bucket index. Why: stable IDs are easier when indexes do not go negative.
    lon_bucket = math.floor((longitude + 180) / grid_size_degrees)  # What: shifts longitude to a non-negative bucket index. Why: west-longitude locations still get sortable positive IDs.
    return f"GRID_{lat_bucket:05d}_{lon_bucket:05d}"


def _smoke_test() -> None:
    """Run with: python -m src.transform"""
    # What: transforms one representative accepted record. Why: this gives a quick check for metadata, availability, and grid assignment.
    timestamp = datetime(2026, 8, 30, 12, 3, tzinfo=timezone.utc)
    records = [
        {
            "vehicle_id": "bike-1",
            "vehicle_type_id": "scooter",
            "latitude": 47.61,
            "longitude": -122.33,
            "is_reserved": False,
            "is_disabled": False,
            "raw_payload": {"bike_id": "bike-1"},
        }
    ]
    transformed = transform_records(records, "smoke-run", timestamp, timestamp, "smoke_feed", 0.01, "SUCCESS")
    print("Transform smoke test passed")
    print(transformed[0])


if __name__ == "__main__":
    _smoke_test()
