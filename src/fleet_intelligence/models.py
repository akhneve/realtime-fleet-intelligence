"""Typed objects passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type QualityStatus = Literal["SUCCESS", "WARNING", "FAILED"]
type VolumeDropPolicy = Literal["WARNING", "FAILED"]
type FeedName = Literal[
    "system_information",
    "station_information",
    "station_status",
    "free_bike_status",
]


@dataclass(frozen=True, slots=True)
class ExtractResult:
    payload: JsonObject
    http_status: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class NormalizedVehicle:
    vehicle_id: str
    vehicle_type_id: str | None
    latitude: float
    longitude: float
    is_reserved: bool
    is_disabled: bool


@dataclass(frozen=True, slots=True)
class NormalizedSystemInformation:
    system_id: str
    name: str
    language: str
    timezone: str
    license_url: str | None
    attribution_organization_name: str | None


@dataclass(frozen=True, slots=True)
class NormalizedStationInformation:
    station_id: str
    name: str
    short_name: str | None
    latitude: float
    longitude: float
    region_id: str | None
    capacity: int | None


@dataclass(frozen=True, slots=True)
class NormalizedStationStatus:
    station_id: str
    num_vehicles_available: int
    num_docks_available: int
    is_installed: bool
    is_renting: bool
    is_returning: bool
    last_reported: datetime | None


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    record_key: str | None
    raw_payload: JsonObject
    reason_code: str
    reason_detail: str
    feed_name: FeedName = "free_bike_status"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    source_timestamp: datetime | None
    records_received: int
    accepted_records: list[NormalizedVehicle]
    rejected_records: list[RejectedRecord]
    observed_schema_keys: list[str]


@dataclass(frozen=True, slots=True)
class SystemInformationValidationResult:
    source_timestamp: datetime | None
    record: NormalizedSystemInformation
    observed_schema_keys: list[str]


@dataclass(frozen=True, slots=True)
class StationInformationValidationResult:
    source_timestamp: datetime | None
    records_received: int
    accepted_records: list[NormalizedStationInformation]
    rejected_records: list[RejectedRecord]
    observed_schema_keys: list[str]


@dataclass(frozen=True, slots=True)
class StationStatusValidationResult:
    source_timestamp: datetime | None
    records_received: int
    accepted_records: list[NormalizedStationStatus]
    rejected_records: list[RejectedRecord]
    observed_schema_keys: list[str]


@dataclass(frozen=True, slots=True)
class VehicleSnapshot:
    vehicle_id: str
    vehicle_type_id: str | None
    latitude: float
    longitude: float
    is_reserved: bool
    is_disabled: bool
    available_flag: bool
    grid_id: str
    run_id: str
    snapshot_timestamp: datetime
    source_timestamp: datetime | None
    ingestion_timestamp: datetime
    quality_flag: QualityStatus


@dataclass(frozen=True, slots=True)
class SystemInformationRecord:
    system_id: str
    name: str
    language: str
    timezone: str
    license_url: str | None
    attribution_organization_name: str | None
    source_timestamp: datetime | None
    ingestion_timestamp: datetime
    run_id: str


@dataclass(frozen=True, slots=True)
class StationInformationRecord:
    station_id: str
    name: str
    short_name: str | None
    latitude: float
    longitude: float
    region_id: str | None
    capacity: int | None
    source_timestamp: datetime | None
    ingestion_timestamp: datetime
    run_id: str


@dataclass(frozen=True, slots=True)
class StationStatusSnapshot:
    station_id: str
    num_vehicles_available: int
    num_docks_available: int
    is_installed: bool
    is_renting: bool
    is_returning: bool
    last_reported: datetime | None
    snapshot_timestamp: datetime
    source_timestamp: datetime | None
    ingestion_timestamp: datetime
    run_id: str
    quality_flag: QualityStatus


@dataclass(frozen=True, slots=True)
class FeedRunMetric:
    feed_name: FeedName
    source_url: str
    http_status: int
    api_latency_ms: int
    source_timestamp: datetime | None
    records_received: int
    records_valid: int
    records_rejected: int
    observed_schema_keys: list[str]


@dataclass(frozen=True, slots=True)
class GridAggregate:
    bucket_timestamp: datetime
    grid_id: str
    total_vehicles: int
    available_vehicles: int
    unavailable_vehicles: int
    run_id: str


@dataclass(slots=True)
class PipelineRun:
    run_id: str
    started_at: datetime
    completed_at: datetime
    http_status: int | None
    records_received: int
    records_valid: int
    records_rejected: int
    api_latency_ms: int | None
    pipeline_duration_ms: int
    quality_status: QualityStatus
    error_message: str | None
    observed_schema_keys: list[str]
