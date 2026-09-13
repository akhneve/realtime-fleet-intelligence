"""Deterministic transformations from normalized vehicles to database snapshots."""

from __future__ import annotations

import math
from datetime import datetime

from .models import (
    NormalizedStationInformation,
    NormalizedStationStatus,
    NormalizedSystemInformation,
    NormalizedVehicle,
    QualityStatus,
    StationInformationRecord,
    StationStatusSnapshot,
    SystemInformationRecord,
    VehicleSnapshot,
)


def transform_system_information(
    record: NormalizedSystemInformation,
    *,
    run_id: str,
    source_timestamp: datetime | None,
    ingestion_timestamp: datetime,
) -> SystemInformationRecord:
    return SystemInformationRecord(
        system_id=record.system_id,
        name=record.name,
        language=record.language,
        timezone=record.timezone,
        license_url=record.license_url,
        attribution_organization_name=record.attribution_organization_name,
        source_timestamp=source_timestamp,
        ingestion_timestamp=ingestion_timestamp,
        run_id=run_id,
    )


def transform_station_information(
    records: list[NormalizedStationInformation],
    *,
    run_id: str,
    source_timestamp: datetime | None,
    ingestion_timestamp: datetime,
) -> list[StationInformationRecord]:
    return [
        StationInformationRecord(
            station_id=record.station_id,
            name=record.name,
            short_name=record.short_name,
            latitude=record.latitude,
            longitude=record.longitude,
            region_id=record.region_id,
            capacity=record.capacity,
            source_timestamp=source_timestamp,
            ingestion_timestamp=ingestion_timestamp,
            run_id=run_id,
        )
        for record in records
    ]


def transform_station_status(
    records: list[NormalizedStationStatus],
    *,
    run_id: str,
    source_timestamp: datetime | None,
    ingestion_timestamp: datetime,
    quality_flag: QualityStatus,
) -> list[StationStatusSnapshot]:
    snapshot_timestamp = source_timestamp or ingestion_timestamp
    return [
        StationStatusSnapshot(
            station_id=record.station_id,
            num_vehicles_available=record.num_vehicles_available,
            num_docks_available=record.num_docks_available,
            is_installed=record.is_installed,
            is_renting=record.is_renting,
            is_returning=record.is_returning,
            last_reported=record.last_reported,
            snapshot_timestamp=snapshot_timestamp,
            source_timestamp=source_timestamp,
            ingestion_timestamp=ingestion_timestamp,
            run_id=run_id,
            quality_flag=quality_flag,
        )
        for record in records
    ]


def transform_records(
    records: list[NormalizedVehicle],
    *,
    run_id: str,
    source_timestamp: datetime | None,
    ingestion_timestamp: datetime,
    grid_size_degrees: float,
    quality_flag: QualityStatus,
) -> list[VehicleSnapshot]:
    snapshot_timestamp = source_timestamp or ingestion_timestamp
    return [
        VehicleSnapshot(
            vehicle_id=record.vehicle_id,
            vehicle_type_id=record.vehicle_type_id,
            latitude=record.latitude,
            longitude=record.longitude,
            is_reserved=record.is_reserved,
            is_disabled=record.is_disabled,
            available_flag=derive_available_flag(record.is_reserved, record.is_disabled),
            grid_id=assign_grid_id(record.latitude, record.longitude, grid_size_degrees),
            run_id=run_id,
            snapshot_timestamp=snapshot_timestamp,
            source_timestamp=source_timestamp,
            ingestion_timestamp=ingestion_timestamp,
            quality_flag=quality_flag,
        )
        for record in records
    ]


def derive_available_flag(is_reserved: bool, is_disabled: bool) -> bool:
    return not is_reserved and not is_disabled


def assign_grid_id(latitude: float, longitude: float, grid_size_degrees: float = 0.01) -> str:
    if grid_size_degrees <= 0:
        raise ValueError("grid_size_degrees must be positive")
    lat_bucket = math.floor((latitude + 90) / grid_size_degrees)
    lon_bucket = math.floor((longitude + 180) / grid_size_degrees)
    return f"GRID_{lat_bucket:05d}_{lon_bucket:05d}"
