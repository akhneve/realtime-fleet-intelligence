"""GBFS envelope and record validation for all configured Lime feeds."""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime
from typing import cast

from .config import BoundingBox
from .models import (
    JsonObject,
    JsonValue,
    NormalizedStationInformation,
    NormalizedStationStatus,
    NormalizedSystemInformation,
    NormalizedVehicle,
    RejectedRecord,
    StationInformationValidationResult,
    StationStatusValidationResult,
    SystemInformationValidationResult,
    ValidationResult,
)


def validate_system_information(payload: JsonObject) -> SystemInformationValidationResult:
    """Validate and normalize the singleton ``system_information`` document."""

    data, source_timestamp = _data_object(payload)
    required = {
        name: _required_text(data.get(name), name)
        for name in ("system_id", "name", "language", "timezone")
    }
    record = NormalizedSystemInformation(
        system_id=required["system_id"],
        name=required["name"],
        language=required["language"],
        timezone=required["timezone"],
        license_url=_optional_text(data.get("license_url")),
        attribution_organization_name=_optional_text(data.get("attribution_organization_name")),
    )
    return SystemInformationValidationResult(source_timestamp, record, sorted(data))


def validate_station_information(
    payload: JsonObject, bbox: BoundingBox | None = None
) -> StationInformationValidationResult:
    """Validate station definitions while quarantining malformed individual rows."""

    records, source_timestamp = _station_envelope(payload)
    objects = [record for record in records if isinstance(record, dict)]
    id_counts = Counter(
        station_id for record in objects if (station_id := _station_id(record)) is not None
    )
    accepted: list[NormalizedStationInformation] = []
    rejected: list[RejectedRecord] = []
    observed_keys: set[str] = set()

    for value in records:
        if not isinstance(value, dict):
            rejected.append(
                RejectedRecord(
                    None,
                    {"value": value},
                    "MALFORMED_RECORD",
                    "Station record is not a JSON object",
                    "station_information",
                )
            )
            continue
        raw = value
        observed_keys.update(raw)
        station_id = _station_id(raw)
        if station_id is None:
            rejected.append(
                RejectedRecord(
                    None,
                    raw,
                    "MISSING_ID",
                    "Missing station_id",
                    "station_information",
                )
            )
            continue
        if id_counts[station_id] > 1:
            rejected.append(
                RejectedRecord(
                    station_id,
                    raw,
                    "DUPLICATE_ID",
                    "Duplicate normalized station ID in source snapshot",
                    "station_information",
                )
            )
            continue
        normalized, failure = _normalize_station_information(raw, station_id)
        if failure is not None:
            rejected.append(
                RejectedRecord(station_id, raw, failure[0], failure[1], "station_information")
            )
            continue
        assert normalized is not None
        if bbox is not None and not bbox.contains(normalized.latitude, normalized.longitude):
            rejected.append(
                RejectedRecord(
                    station_id,
                    raw,
                    "OUT_OF_BOUNDS",
                    "Station coordinate is outside configured bounding box",
                    "station_information",
                )
            )
            continue
        accepted.append(normalized)

    return StationInformationValidationResult(
        source_timestamp, len(records), accepted, rejected, sorted(observed_keys)
    )


def validate_station_status(payload: JsonObject) -> StationStatusValidationResult:
    """Validate station availability and operating-state rows."""

    records, source_timestamp = _station_envelope(payload)
    objects = [record for record in records if isinstance(record, dict)]
    id_counts = Counter(
        station_id for record in objects if (station_id := _station_id(record)) is not None
    )
    accepted: list[NormalizedStationStatus] = []
    rejected: list[RejectedRecord] = []
    observed_keys: set[str] = set()

    for value in records:
        if not isinstance(value, dict):
            rejected.append(
                RejectedRecord(
                    None,
                    {"value": value},
                    "MALFORMED_RECORD",
                    "Station status record is not a JSON object",
                    "station_status",
                )
            )
            continue
        raw = value
        observed_keys.update(raw)
        station_id = _station_id(raw)
        if station_id is None:
            rejected.append(
                RejectedRecord(None, raw, "MISSING_ID", "Missing station_id", "station_status")
            )
            continue
        if id_counts[station_id] > 1:
            rejected.append(
                RejectedRecord(
                    station_id,
                    raw,
                    "DUPLICATE_ID",
                    "Duplicate normalized station ID in source snapshot",
                    "station_status",
                )
            )
            continue
        normalized, failure = _normalize_station_status(raw, station_id)
        if failure is not None:
            rejected.append(
                RejectedRecord(station_id, raw, failure[0], failure[1], "station_status")
            )
            continue
        assert normalized is not None
        accepted.append(normalized)

    return StationStatusValidationResult(
        source_timestamp, len(records), accepted, rejected, sorted(observed_keys)
    )


def validate_payload(payload: JsonObject, bbox: BoundingBox | None = None) -> ValidationResult:
    """Validate a GBFS v2 or v3 payload and quarantine invalid records."""

    records, source_timestamp = _validate_envelope(payload)
    accepted, rejected, observed_keys = validate_records(records, bbox)
    return ValidationResult(
        source_timestamp=source_timestamp,
        records_received=len(records),
        accepted_records=accepted,
        rejected_records=rejected,
        observed_schema_keys=observed_keys,
    )


validate_free_bike_status = validate_payload


def validate_records(
    records: list[JsonValue], bbox: BoundingBox | None = None
) -> tuple[list[NormalizedVehicle], list[RejectedRecord], list[str]]:
    """Normalize records while rejecting every copy of a duplicated vehicle ID."""

    objects = [record for record in records if isinstance(record, dict)]
    id_counts = Counter(
        vehicle_id for record in objects if (vehicle_id := _normalized_id(record)) is not None
    )

    accepted: list[NormalizedVehicle] = []
    rejected: list[RejectedRecord] = []
    observed_keys: set[str] = set()

    for value in records:
        if not isinstance(value, dict):
            rejected.append(
                RejectedRecord(
                    None,
                    {"value": value},
                    "MALFORMED_RECORD",
                    "Record is not a JSON object",
                )
            )
            continue

        raw = value
        observed_keys.update(raw)
        vehicle_id = _normalized_id(raw)
        if vehicle_id is None:
            rejected.append(
                RejectedRecord(None, raw, "MISSING_ID", "Missing bike_id or vehicle_id")
            )
            continue
        if id_counts[vehicle_id] > 1:
            rejected.append(
                RejectedRecord(
                    vehicle_id,
                    raw,
                    "DUPLICATE_ID",
                    "Duplicate normalized vehicle ID in source snapshot",
                )
            )
            continue

        normalized, failure = _normalize_record(raw, vehicle_id)
        if failure is not None:
            rejected.append(RejectedRecord(vehicle_id, raw, failure[0], failure[1]))
            continue
        assert normalized is not None
        if bbox is not None and not bbox.contains(normalized.latitude, normalized.longitude):
            rejected.append(
                RejectedRecord(
                    vehicle_id,
                    raw,
                    "OUT_OF_BOUNDS",
                    "Vehicle coordinate is outside configured bounding box",
                )
            )
            continue
        accepted.append(normalized)

    return accepted, rejected, sorted(observed_keys)


def _validate_envelope(payload: JsonObject) -> tuple[list[JsonValue], datetime | None]:
    data, source_timestamp = _data_object(payload)

    # GBFS v3 renamed bikes/free_bike_status to vehicles/vehicle_status.
    records = data.get("vehicles") if "vehicles" in data else data.get("bikes")
    if not isinstance(records, list):
        raise ValueError("Payload is missing GBFS data.vehicles or data.bikes array")
    return records, source_timestamp


def _data_object(payload: JsonObject) -> tuple[JsonObject, datetime | None]:
    if not payload:
        raise ValueError("Payload must be a non-empty JSON object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Payload is missing GBFS data object")
    return data, _parse_epoch_timestamp(payload.get("last_updated"))


def _station_envelope(payload: JsonObject) -> tuple[list[JsonValue], datetime | None]:
    data, source_timestamp = _data_object(payload)
    records = data.get("stations")
    if not isinstance(records, list):
        raise ValueError("Payload is missing GBFS data.stations array")
    return records, source_timestamp


def _normalized_id(raw: JsonObject) -> str | None:
    value = raw.get("bike_id")
    if value is None or not str(value).strip():
        value = raw.get("vehicle_id")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _station_id(raw: JsonObject) -> str | None:
    return _optional_text(raw.get("station_id"))


def _normalize_station_information(
    raw: JsonObject, station_id: str
) -> tuple[NormalizedStationInformation | None, tuple[str, str] | None]:
    name = _optional_text(raw.get("name"))
    if name is None:
        return None, ("MISSING_NAME", "Station name is missing or blank")
    latitude = _finite_float(raw.get("lat") if "lat" in raw else raw.get("latitude"))
    if latitude is None or not -90 <= latitude <= 90:
        return None, ("INVALID_LATITUDE", "Latitude is missing, non-finite, or outside [-90, 90]")
    longitude = _finite_float(raw.get("lon") if "lon" in raw else raw.get("longitude"))
    if longitude is None or not -180 <= longitude <= 180:
        return None, (
            "INVALID_LONGITUDE",
            "Longitude is missing, non-finite, or outside [-180, 180]",
        )
    capacity = _optional_nonnegative_int(raw.get("capacity"))
    if raw.get("capacity") is not None and capacity is None:
        return None, ("INVALID_CAPACITY", "Station capacity must be a non-negative integer")
    return (
        NormalizedStationInformation(
            station_id=station_id,
            name=name,
            short_name=_optional_text(raw.get("short_name")),
            latitude=latitude,
            longitude=longitude,
            region_id=_optional_text(raw.get("region_id")),
            capacity=capacity,
        ),
        None,
    )


def _normalize_station_status(
    raw: JsonObject, station_id: str
) -> tuple[NormalizedStationStatus | None, tuple[str, str] | None]:
    available_value = (
        raw.get("num_vehicles_available")
        if "num_vehicles_available" in raw
        else raw.get("num_bikes_available")
    )
    available = _optional_nonnegative_int(available_value)
    docks = _optional_nonnegative_int(raw.get("num_docks_available"))
    if available is None or docks is None:
        return None, (
            "INVALID_COUNTS",
            "Available vehicle and dock counts must be non-negative integers",
        )
    states = tuple(
        _optional_bool(raw.get(name)) for name in ("is_installed", "is_renting", "is_returning")
    )
    if any(state is None for state in states):
        return None, (
            "INVALID_STATE",
            "is_installed, is_renting, and is_returning must all be boolean",
        )
    last_reported_value = raw.get("last_reported")
    last_reported = _parse_epoch_timestamp(last_reported_value)
    if last_reported_value is not None and last_reported is None:
        return None, ("INVALID_TIMESTAMP", "last_reported must be a Unix timestamp")
    is_installed, is_renting, is_returning = cast(tuple[bool, bool, bool], states)
    return (
        NormalizedStationStatus(
            station_id,
            available,
            docks,
            is_installed,
            is_renting,
            is_returning,
            last_reported,
        ),
        None,
    )


def _normalize_record(
    raw: JsonObject, vehicle_id: str
) -> tuple[NormalizedVehicle | None, tuple[str, str] | None]:
    latitude = raw.get("lat") if "lat" in raw else raw.get("latitude")
    longitude = raw.get("lon") if "lon" in raw else raw.get("longitude")

    latitude_value = _finite_float(latitude)
    if latitude_value is None or not -90 <= latitude_value <= 90:
        return None, ("INVALID_LATITUDE", "Latitude is missing, non-finite, or outside [-90, 90]")
    longitude_value = _finite_float(longitude)
    if longitude_value is None or not -180 <= longitude_value <= 180:
        return None, (
            "INVALID_LONGITUDE",
            "Longitude is missing, non-finite, or outside [-180, 180]",
        )

    is_reserved = _optional_bool(raw.get("is_reserved"))
    is_disabled = _optional_bool(raw.get("is_disabled"))
    if is_reserved is None or is_disabled is None:
        return None, (
            "INVALID_STATE",
            "is_reserved and is_disabled must both be present and boolean",
        )

    vehicle_type = raw.get("vehicle_type_id")
    if vehicle_type is None:
        vehicle_type = raw.get("vehicle_type")
    vehicle_type_id = str(vehicle_type).strip() if vehicle_type is not None else None

    return (
        NormalizedVehicle(
            vehicle_id=vehicle_id,
            vehicle_type_id=vehicle_type_id or None,
            latitude=latitude_value,
            longitude=longitude_value,
            is_reserved=is_reserved,
            is_disabled=is_disabled,
        ),
        None,
    )


def _finite_float(value: JsonValue) -> float | None:
    try:
        number = float(cast(str | int | float, value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_nonnegative_int(value: JsonValue) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(cast(str | int | float, value))
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, str) and str(number) != value.strip():
        return None
    return number if number >= 0 else None


def _optional_text(value: JsonValue) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _required_text(value: JsonValue, field_name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError(f"GBFS data.{field_name} must be a non-empty string")
    return normalized


def _optional_bool(value: JsonValue) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _parse_epoch_timestamp(value: JsonValue) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(cast(str | int | float, value)), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
