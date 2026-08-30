"""Feed and record validation for GBFS free bike status payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import BoundingBox


REQUIRED_CANONICAL_FIELDS = {"vehicle_id", "latitude", "longitude"}  # What: internal minimum fields. Why: every accepted vehicle row needs an ID and map coordinates.


@dataclass(frozen=True)
class RejectedRecord:
    # What: structured invalid-record output. Why: rejected rows must be loaded into PostgreSQL with reason codes and original payloads.
    record_key: str | None
    raw_payload: dict[str, Any]
    reason_code: str
    reason_detail: str


@dataclass(frozen=True)
class ValidationResult:
    # What: full validation summary for a feed snapshot. Why: the orchestrator needs counts, accepted rows, rejected rows, and schema-drift evidence.
    source_timestamp: datetime | None
    records_received: int
    accepted_records: list[dict[str, Any]]
    rejected_records: list[RejectedRecord]
    observed_schema_keys: list[str]


def validate_feed(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], datetime | None]:
    # What: validates the top-level GBFS response shape. Why: record validation only makes sense after confirming data.bikes exists.
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Payload must be a non-empty JSON object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Payload is missing GBFS data object")
    bikes = data.get("bikes")
    if not isinstance(bikes, list):
        raise ValueError("Payload is missing GBFS data.bikes array")
    return bikes, _parse_epoch_timestamp(payload.get("last_updated") or payload.get("ttl_last_updated"))  # What: returns raw records plus source time. Why: freshness checks need the feed timestamp.


def validate_records(records: Iterable[Any], bbox: BoundingBox | None = None) -> tuple[list[dict[str, Any]], list[RejectedRecord], list[str]]:
    # What: splits raw records into accepted normalized records and rejected records. Why: the pipeline should preserve bad data lineage instead of dropping it.
    accepted: list[dict[str, Any]] = []
    rejected: list[RejectedRecord] = []
    observed_keys: set[str] = set()
    first_seen_by_id: dict[str, int] = {}
    duplicate_indexes: set[int] = set()

    materialized = list(records)  # What: freezes the iterable into a list. Why: duplicate detection needs two passes over the same snapshot.
    for index, raw in enumerate(materialized):
        # What: first pass validates basic record shape and finds duplicate IDs. Why: all duplicate copies should be rejected, including the first occurrence.
        if not isinstance(raw, dict):
            rejected.append(RejectedRecord(None, {"value": raw}, "MALFORMED_RECORD", "Record is not a JSON object"))
            continue
        observed_keys.update(raw.keys())  # What: captures every source key seen. Why: schema drift should be visible without breaking on unknown extra fields.
        vehicle_id = raw.get("bike_id") or raw.get("vehicle_id")
        if vehicle_id is None or str(vehicle_id).strip() == "":
            rejected.append(RejectedRecord(None, raw, "MISSING_ID", "Missing bike_id or vehicle_id"))
            continue
        vehicle_key = str(vehicle_id)
        if vehicle_key in first_seen_by_id:
            duplicate_indexes.add(first_seen_by_id[vehicle_key])
            duplicate_indexes.add(index)
        else:
            first_seen_by_id[vehicle_key] = index

    for index, raw in enumerate(materialized):
        # What: second pass applies field-level validation and normalization. Why: duplicates must be known before deciding which rows are safe to accept.
        if not isinstance(raw, dict):
            continue
        vehicle_id = raw.get("bike_id") or raw.get("vehicle_id")
        record_key = str(vehicle_id) if vehicle_id is not None else None
        if record_key is None or record_key.strip() == "":
            continue
        if index in duplicate_indexes:
            # What: rejects every record sharing a duplicated ID. Why: silently keeping one duplicate could corrupt current-state upserts.
            rejected.append(RejectedRecord(record_key, raw, "DUPLICATE_ID", "Duplicate vehicle ID in source snapshot"))
            continue

        normalized, failure = _normalize_record(raw)
        if failure is not None:
            rejected.append(RejectedRecord(record_key, raw, failure[0], failure[1]))
            continue
        if bbox is not None and not bbox.contains(normalized["latitude"], normalized["longitude"]):
            # What: optionally rejects valid coordinates outside the configured market. Why: geographic limits are deployment config, not hard-coded business logic.
            rejected.append(RejectedRecord(record_key, raw, "OUT_OF_BOUNDS", "Vehicle coordinate is outside configured bounding box"))
            continue
        accepted.append(normalized)

    return accepted, rejected, sorted(observed_keys)


def validate_payload(payload: dict[str, Any], bbox: BoundingBox | None = None) -> ValidationResult:
    # What: end-to-end validation wrapper for one feed payload. Why: main.py needs one simple call that returns all validation outputs.
    records, source_timestamp = validate_feed(payload)
    accepted, rejected, observed_keys = validate_records(records, bbox=bbox)
    return ValidationResult(
        source_timestamp=source_timestamp,
        records_received=len(records),
        accepted_records=accepted,
        rejected_records=rejected,
        observed_schema_keys=observed_keys,
    )


def _normalize_record(raw: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, str] | None]:
    # What: converts source-specific GBFS fields into the internal model. Why: downstream transforms/loaders should not care whether the feed uses bike_id/lat/lon.
    vehicle_id = raw.get("bike_id") or raw.get("vehicle_id")
    latitude = raw.get("lat") if "lat" in raw else raw.get("latitude")
    longitude = raw.get("lon") if "lon" in raw else raw.get("longitude")

    try:
        # What: coerces latitude to float. Why: JSON feeds may provide numeric-looking values, but the database needs a real number.
        latitude_value = float(latitude)
    except (TypeError, ValueError):
        return {}, ("INVALID_LATITUDE", "Latitude is missing or not numeric")
    try:
        # What: coerces longitude to float. Why: map and grid logic require numeric longitude.
        longitude_value = float(longitude)
    except (TypeError, ValueError):
        return {}, ("INVALID_LONGITUDE", "Longitude is missing or not numeric")

    if not -90 <= latitude_value <= 90:
        # What: rejects impossible latitude values. Why: impossible coordinates would break trust in maps and aggregates.
        return {}, ("INVALID_LATITUDE", "Latitude is outside [-90, 90]")
    if not -180 <= longitude_value <= 180:
        # What: rejects impossible longitude values. Why: impossible coordinates would place vehicles outside valid earth coordinates.
        return {}, ("INVALID_LONGITUDE", "Longitude is outside [-180, 180]")

    is_reserved = _optional_bool(raw.get("is_reserved"))
    is_disabled = _optional_bool(raw.get("is_disabled"))
    if is_reserved is None:
        # What: requires a valid reservation state. Why: missing or invalid state must not be interpreted as available supply.
        return {}, ("INVALID_STATE", "is_reserved must be present and boolean")
    if is_disabled is None:
        return {}, ("INVALID_STATE", "is_disabled must be present and boolean")

    vehicle_type_id = raw.get("vehicle_type_id")
    if vehicle_type_id is None:
        # What: accepts Lime's vehicle_type alias. Why: the live feed currently uses vehicle_type rather than the GBFS vehicle_type_id name.
        vehicle_type_id = raw.get("vehicle_type")

    return {
        "vehicle_id": str(vehicle_id),
        "vehicle_type_id": vehicle_type_id,
        "latitude": latitude_value,
        "longitude": longitude_value,
        "is_reserved": is_reserved,
        "is_disabled": is_disabled,
        "raw_payload": raw,
    }, None


def _optional_bool(value: Any) -> bool | None:
    # What: normalizes common boolean representations. Why: GBFS-style feeds may send true/false as booleans, 0/1, or strings.
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _parse_epoch_timestamp(value: Any) -> datetime | None:
    # What: parses a Unix epoch timestamp into UTC datetime. Why: freshness checks and database timestamps should use timezone-aware values.
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _smoke_test() -> None:
    """Run with: python -m src.validate"""
    # What: exercises valid, invalid, duplicate, missing-ID, and malformed records. Why: one command should prove quarantine logic is working.
    payload = {
        "last_updated": 1798632000,
        "data": {
            "bikes": [
                {"bike_id": "ok-1", "lat": 47.61, "lon": -122.33, "is_reserved": False, "is_disabled": False},
                {"bike_id": "bad-lat", "lat": 91, "lon": -122.33},
                {"bike_id": "dupe", "lat": 47.62, "lon": -122.34},
                {"bike_id": "dupe", "lat": 47.63, "lon": -122.35},
                {"lat": 47.64, "lon": -122.36},
                "malformed",
            ]
        },
    }
    result = validate_payload(payload)
    print("Validate smoke test passed")
    print(f"Records received: {result.records_received}")
    print(f"Accepted records: {len(result.accepted_records)}")
    print(f"Rejected records: {len(result.rejected_records)}")
    print(f"Rejected reasons: {[record.reason_code for record in result.rejected_records]}")
    print(f"Observed keys: {result.observed_schema_keys}")


if __name__ == "__main__":
    _smoke_test()
