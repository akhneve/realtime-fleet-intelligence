"""Transactional PostgreSQL persistence for one fleet snapshot."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .config import DatabaseSettings
from .metrics import build_grid_15min_rows
from .models import (
    FeedRunMetric,
    GridAggregate,
    PipelineRun,
    RejectedRecord,
    StationInformationRecord,
    StationStatusSnapshot,
    SystemInformationRecord,
    VehicleSnapshot,
)

type DbConnection = psycopg.Connection[Any]


def connect(settings: DatabaseSettings) -> DbConnection:
    """Open a hardened connection; explicit transaction blocks own every write."""

    return psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.name,
        user=settings.user,
        password=settings.password,
        sslmode=settings.sslmode,
        connect_timeout=settings.connect_timeout_seconds,
        options=(
            f"-c statement_timeout={settings.statement_timeout_seconds * 1000} "
            "-c application_name=realtime-fleet-intelligence"
        ),
        autocommit=True,
    )


def get_recent_baseline_count(conn: DbConnection) -> int | None:
    """Average the last day of usable 15-minute runs for volume anomaly detection."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            select avg(records_valid)::int
            from (
                select records_valid
                from pipeline_runs
                where quality_status in ('SUCCESS', 'WARNING')
                  and records_valid > 0
                order by started_at desc
                limit 96
            ) recent
            """
        )
        row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def load_snapshot(
    conn: DbConnection,
    *,
    records: list[VehicleSnapshot],
    rejected_records: list[RejectedRecord],
    pipeline_run: PipelineRun,
    detail_retention_hours: int,
    aggregate_retention_days: int,
    pipeline_run_retention_days: int,
    rejected_retention_days: int,
    rebalance_high_threshold: int,
    rebalance_medium_threshold: int,
    system_information: SystemInformationRecord | None = None,
    station_information: list[StationInformationRecord] | None = None,
    station_status: list[StationStatusSnapshot] | None = None,
    feed_metrics: list[FeedRunMetric] | None = None,
) -> None:
    """Atomically persist all four GBFS datasets and their quality evidence."""

    aggregates = build_grid_15min_rows(records) if pipeline_run.quality_status != "FAILED" else []
    with conn.transaction():
        if records:
            _stage_vehicle_snapshots(conn, records)
            _insert_vehicle_snapshots(conn)
        if station_status:
            _insert_station_status_snapshots(conn, station_status)
        if pipeline_run.quality_status != "FAILED":
            if records:
                _upsert_current_state(conn)
            # A usable full snapshot is authoritative: absent vehicles are stale.
            _delete_stale_current_state(conn, pipeline_run.run_id)
            _replace_grid_aggregates(conn, aggregates)
            if system_information is not None:
                _upsert_system_information(conn, system_information)
            if station_information is not None:
                _replace_station_information(conn, station_information, pipeline_run.run_id)
            if station_status is not None:
                _replace_current_station_status(conn, station_status, pipeline_run.run_id)
        _insert_rejected_records(conn, pipeline_run.run_id, rejected_records)
        _insert_feed_metrics(conn, pipeline_run.run_id, feed_metrics or [])
        _upsert_reporting_thresholds(conn, rebalance_high_threshold, rebalance_medium_threshold)
        _apply_retention(
            conn,
            detail_retention_hours,
            aggregate_retention_days,
            pipeline_run_retention_days,
            rejected_retention_days,
        )
        _insert_pipeline_run(conn, pipeline_run)


def log_failed_run(conn: DbConnection, pipeline_run: PipelineRun) -> None:
    with conn.transaction():
        _insert_pipeline_run(conn, pipeline_run)


def upsert_pipeline_run(conn: DbConnection, pipeline_run: PipelineRun) -> None:
    """Refresh final timing after the snapshot transaction has committed."""

    with conn.transaction():
        _insert_pipeline_run(conn, pipeline_run)


def _stage_vehicle_snapshots(conn: DbConnection, records: list[VehicleSnapshot]) -> None:
    """COPY each accepted vehicle once, then reuse the stage for all set-based writes."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            create temporary table fleet_snapshot_stage (
                vehicle_id text not null,
                vehicle_type_id text,
                latitude double precision not null,
                longitude double precision not null,
                is_reserved boolean not null,
                is_disabled boolean not null,
                available_flag boolean not null,
                grid_id text not null,
                run_id uuid not null,
                snapshot_timestamp timestamptz not null,
                source_timestamp timestamptz,
                ingestion_timestamp timestamptz not null,
                quality_flag text not null
            ) on commit drop
            """
        )
        with cursor.copy(
            """
            copy fleet_snapshot_stage (
                vehicle_id, vehicle_type_id, latitude, longitude,
                is_reserved, is_disabled, available_flag, grid_id, run_id,
                snapshot_timestamp, source_timestamp, ingestion_timestamp, quality_flag
            ) from stdin
            """
        ) as copy:
            for record in records:
                copy.write_row(
                    (
                        record.vehicle_id,
                        record.vehicle_type_id,
                        record.latitude,
                        record.longitude,
                        record.is_reserved,
                        record.is_disabled,
                        record.available_flag,
                        record.grid_id,
                        record.run_id,
                        record.snapshot_timestamp,
                        record.source_timestamp,
                        record.ingestion_timestamp,
                        record.quality_flag,
                    )
                )


def _insert_vehicle_snapshots(conn: DbConnection) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            insert into fact_vehicle_snapshot (
                snapshot_timestamp, vehicle_id, latitude, longitude,
                available_flag, grid_id, run_id, quality_flag
            )
            select snapshot_timestamp, vehicle_id, latitude, longitude,
                   available_flag, grid_id, run_id, quality_flag
            from fleet_snapshot_stage
            on conflict (run_id, vehicle_id) do nothing
            """
        )


def _upsert_current_state(conn: DbConnection) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            insert into current_vehicle_state (
                vehicle_id, vehicle_type_id, latitude, longitude, is_reserved,
                is_disabled, available_flag, grid_id, source_timestamp,
                ingestion_timestamp, run_id
            )
            select vehicle_id, vehicle_type_id, latitude, longitude, is_reserved,
                   is_disabled, available_flag, grid_id, source_timestamp,
                   ingestion_timestamp, run_id
            from fleet_snapshot_stage
            on conflict (vehicle_id) do update set
                vehicle_type_id = excluded.vehicle_type_id,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                is_reserved = excluded.is_reserved,
                is_disabled = excluded.is_disabled,
                available_flag = excluded.available_flag,
                grid_id = excluded.grid_id,
                source_timestamp = excluded.source_timestamp,
                ingestion_timestamp = excluded.ingestion_timestamp,
                run_id = excluded.run_id
            """
        )


def _delete_stale_current_state(conn: DbConnection, run_id: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute("delete from current_vehicle_state where run_id <> %s", (run_id,))


def _insert_rejected_records(
    conn: DbConnection, run_id: str, rejected_records: list[RejectedRecord]
) -> None:
    if not rejected_records:
        return
    rows = [
        (
            run_id,
            rejected.record_key,
            Jsonb(rejected.raw_payload),
            rejected.reason_code,
            rejected.reason_detail,
            rejected.feed_name,
        )
        for rejected in rejected_records
    ]
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            insert into rejected_records (
                run_id, record_key, raw_payload, reason_code, reason_detail, feed_name
            ) values (%s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def _upsert_system_information(conn: DbConnection, record: SystemInformationRecord) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            insert into system_information (
                system_id, name, language, timezone, license_url,
                attribution_organization_name, source_timestamp,
                ingestion_timestamp, run_id
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (system_id) do update set
                name = excluded.name,
                language = excluded.language,
                timezone = excluded.timezone,
                license_url = excluded.license_url,
                attribution_organization_name = excluded.attribution_organization_name,
                source_timestamp = excluded.source_timestamp,
                ingestion_timestamp = excluded.ingestion_timestamp,
                run_id = excluded.run_id
            """,
            (
                record.system_id,
                record.name,
                record.language,
                record.timezone,
                record.license_url,
                record.attribution_organization_name,
                record.source_timestamp,
                record.ingestion_timestamp,
                record.run_id,
            ),
        )
        cursor.execute("delete from system_information where run_id <> %s", (record.run_id,))


def _replace_station_information(
    conn: DbConnection, records: list[StationInformationRecord], run_id: str
) -> None:
    rows = [
        (
            record.station_id,
            record.name,
            record.short_name,
            record.latitude,
            record.longitude,
            record.region_id,
            record.capacity,
            record.source_timestamp,
            record.ingestion_timestamp,
            record.run_id,
        )
        for record in records
    ]
    with conn.cursor() as cursor:
        if rows:
            cursor.executemany(
                """
                insert into station_information (
                    station_id, name, short_name, latitude, longitude, region_id,
                    capacity, source_timestamp, ingestion_timestamp, run_id
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (station_id) do update set
                    name = excluded.name,
                    short_name = excluded.short_name,
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    region_id = excluded.region_id,
                    capacity = excluded.capacity,
                    source_timestamp = excluded.source_timestamp,
                    ingestion_timestamp = excluded.ingestion_timestamp,
                    run_id = excluded.run_id
                """,
                rows,
            )
        cursor.execute("delete from station_information where run_id <> %s", (run_id,))


def _insert_station_status_snapshots(
    conn: DbConnection, records: list[StationStatusSnapshot]
) -> None:
    rows = [_station_status_row(record) for record in records]
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            insert into fact_station_status_snapshot (
                station_id, num_vehicles_available, num_docks_available,
                is_installed, is_renting, is_returning, last_reported,
                snapshot_timestamp, source_timestamp, ingestion_timestamp,
                run_id, quality_flag
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id, station_id) do nothing
            """,
            rows,
        )


def _replace_current_station_status(
    conn: DbConnection, records: list[StationStatusSnapshot], run_id: str
) -> None:
    rows = [_station_status_row(record)[:-1] for record in records]
    with conn.cursor() as cursor:
        if rows:
            cursor.executemany(
                """
                insert into current_station_status (
                    station_id, num_vehicles_available, num_docks_available,
                    is_installed, is_renting, is_returning, last_reported,
                    snapshot_timestamp, source_timestamp, ingestion_timestamp, run_id
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (station_id) do update set
                    num_vehicles_available = excluded.num_vehicles_available,
                    num_docks_available = excluded.num_docks_available,
                    is_installed = excluded.is_installed,
                    is_renting = excluded.is_renting,
                    is_returning = excluded.is_returning,
                    last_reported = excluded.last_reported,
                    snapshot_timestamp = excluded.snapshot_timestamp,
                    source_timestamp = excluded.source_timestamp,
                    ingestion_timestamp = excluded.ingestion_timestamp,
                    run_id = excluded.run_id
                """,
                rows,
            )
        cursor.execute("delete from current_station_status where run_id <> %s", (run_id,))


def _station_status_row(record: StationStatusSnapshot) -> tuple[object, ...]:
    return (
        record.station_id,
        record.num_vehicles_available,
        record.num_docks_available,
        record.is_installed,
        record.is_renting,
        record.is_returning,
        record.last_reported,
        record.snapshot_timestamp,
        record.source_timestamp,
        record.ingestion_timestamp,
        record.run_id,
        record.quality_flag,
    )


def _insert_feed_metrics(
    conn: DbConnection, run_id: str, feed_metrics: list[FeedRunMetric]
) -> None:
    if not feed_metrics:
        return
    rows = [
        (
            run_id,
            metric.feed_name,
            metric.source_url,
            metric.http_status,
            metric.api_latency_ms,
            metric.source_timestamp,
            metric.records_received,
            metric.records_valid,
            metric.records_rejected,
            Jsonb(metric.observed_schema_keys),
        )
        for metric in feed_metrics
    ]
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            insert into feed_run_metrics (
                run_id, feed_name, source_url, http_status, api_latency_ms,
                source_timestamp, records_received, records_valid,
                records_rejected, observed_schema_keys
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id, feed_name) do update set
                source_url = excluded.source_url,
                http_status = excluded.http_status,
                api_latency_ms = excluded.api_latency_ms,
                source_timestamp = excluded.source_timestamp,
                records_received = excluded.records_received,
                records_valid = excluded.records_valid,
                records_rejected = excluded.records_rejected,
                observed_schema_keys = excluded.observed_schema_keys
            """,
            rows,
        )


def _replace_grid_aggregates(conn: DbConnection, aggregates: list[GridAggregate]) -> None:
    if not aggregates:
        return
    rows = [
        (
            row.bucket_timestamp,
            row.grid_id,
            row.total_vehicles,
            row.available_vehicles,
            row.unavailable_vehicles,
            row.run_id,
        )
        for row in aggregates
    ]
    bucket_timestamps = sorted({row.bucket_timestamp for row in aggregates})
    with conn.cursor() as cursor:
        cursor.execute(
            "delete from fact_grid_15min where bucket_timestamp = any(%s)",
            (bucket_timestamps,),
        )
        cursor.executemany(
            """
            insert into fact_grid_15min (
                bucket_timestamp, grid_id, total_vehicles, available_vehicles,
                unavailable_vehicles, run_id
            ) values (%s, %s, %s, %s, %s, %s)
            on conflict (bucket_timestamp, grid_id) do update set
                total_vehicles = excluded.total_vehicles,
                available_vehicles = excluded.available_vehicles,
                unavailable_vehicles = excluded.unavailable_vehicles,
                run_id = excluded.run_id
            """,
            rows,
        )


def _upsert_reporting_thresholds(
    conn: DbConnection, high_threshold: int, medium_threshold: int
) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            insert into reporting_thresholds (setting_name, setting_value, updated_at)
            values (%s, %s, now())
            on conflict (setting_name) do update set
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
            """,
            [
                ("rebalance_high_threshold", high_threshold),
                ("rebalance_medium_threshold", medium_threshold),
            ],
        )


def _apply_retention(
    conn: DbConnection,
    detail_retention_hours: int,
    aggregate_retention_days: int,
    pipeline_run_retention_days: int,
    rejected_retention_days: int,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "delete from fact_vehicle_snapshot "
            "where snapshot_timestamp < now() - %s * interval '1 hour'",
            (detail_retention_hours,),
        )
        cursor.execute(
            "delete from fact_grid_15min where bucket_timestamp < now() - %s * interval '1 day'",
            (aggregate_retention_days,),
        )
        cursor.execute(
            "delete from fact_station_status_snapshot "
            "where snapshot_timestamp < now() - %s * interval '1 day'",
            (aggregate_retention_days,),
        )
        cursor.execute(
            "delete from rejected_records where rejected_at < now() - %s * interval '1 day'",
            (rejected_retention_days,),
        )
        cursor.execute(
            "delete from pipeline_runs where started_at < now() - %s * interval '1 day'",
            (pipeline_run_retention_days,),
        )


def _insert_pipeline_run(conn: DbConnection, run: PipelineRun) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            insert into pipeline_runs (
                run_id, started_at, completed_at, http_status, records_received,
                records_valid, records_rejected, api_latency_ms,
                pipeline_duration_ms, quality_status, error_message,
                observed_schema_keys
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id) do update set
                completed_at = excluded.completed_at,
                http_status = excluded.http_status,
                records_received = excluded.records_received,
                records_valid = excluded.records_valid,
                records_rejected = excluded.records_rejected,
                api_latency_ms = excluded.api_latency_ms,
                pipeline_duration_ms = excluded.pipeline_duration_ms,
                quality_status = excluded.quality_status,
                error_message = excluded.error_message,
                observed_schema_keys = excluded.observed_schema_keys
            """,
            (
                run.run_id,
                run.started_at,
                run.completed_at,
                run.http_status,
                run.records_received,
                run.records_valid,
                run.records_rejected,
                run.api_latency_ms,
                run.pipeline_duration_ms,
                run.quality_status,
                run.error_message,
                Jsonb(run.observed_schema_keys),
            ),
        )
