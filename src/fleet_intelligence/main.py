"""Command-line orchestration for production ingestion and no-write dry runs."""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from datetime import UTC, datetime

from .config import (
    DatabaseSettings,
    load_database_settings,
    load_pipeline_settings,
)
from .extract import fetch_gbfs_feeds
from .load import (
    DbConnection,
    connect,
    get_recent_baseline_count,
    load_snapshot,
    log_failed_run,
    upsert_pipeline_run,
)
from .metrics import calculate_quality_status
from .models import FeedName, FeedRunMetric, PipelineRun, QualityStatus
from .transform import (
    transform_records,
    transform_station_information,
    transform_station_status,
    transform_system_information,
)
from .validate import (
    validate_free_bike_status,
    validate_station_information,
    validate_station_status,
    validate_system_information,
)

LOGGER = logging.getLogger(__name__)
MAX_ERROR_LENGTH = 2_000


def run() -> int:
    """Execute one production snapshot and return a process exit code."""

    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    timer_started = time.perf_counter()
    conn: DbConnection | None = None
    database_settings: DatabaseSettings | None = None
    pipeline_run: PipelineRun | None = None
    http_status: int | None = None
    api_latency_ms: int | None = None
    records_received = 0
    records_valid = 0
    records_rejected = 0
    observed_schema_keys: list[str] = []

    try:
        LOGGER.info("run_id=%s stage=start", run_id)
        pipeline_settings = load_pipeline_settings()
        database_settings = load_database_settings()
        conn = connect(database_settings)
        baseline_count = get_recent_baseline_count(conn)

        LOGGER.info("run_id=%s stage=extract", run_id)
        feed_urls = pipeline_settings.feed_urls
        extract_results = fetch_gbfs_feeds(feed_urls)
        free_bike_extract = extract_results["free_bike_status"]
        http_status = free_bike_extract.http_status
        api_latency_ms = sum(result.latency_ms for result in extract_results.values())

        LOGGER.info("run_id=%s stage=validate", run_id)
        system_validation = validate_system_information(
            extract_results["system_information"].payload
        )
        station_information_validation = validate_station_information(
            extract_results["station_information"].payload, pipeline_settings.bbox
        )
        station_status_validation = validate_station_status(
            extract_results["station_status"].payload
        )
        vehicle_validation = validate_free_bike_status(
            free_bike_extract.payload, pipeline_settings.bbox
        )
        records_received = vehicle_validation.records_received
        records_valid = len(vehicle_validation.accepted_records)
        all_rejected = [
            *station_information_validation.rejected_records,
            *station_status_validation.rejected_records,
            *vehicle_validation.rejected_records,
        ]
        # The long-standing pipeline summary remains vehicle-scoped so its
        # received = valid + rejected invariant stays stable. Per-feed counts
        # (including station rejects) are recorded in feed_run_metrics.
        records_rejected = len(vehicle_validation.rejected_records)
        observed_schema_keys = vehicle_validation.observed_schema_keys
        assessed_at = datetime.now(UTC)
        quality_status, quality_reasons = calculate_quality_status(
            now=assessed_at,
            source_timestamp=vehicle_validation.source_timestamp,
            feed_stale_minutes=pipeline_settings.feed_stale_minutes,
            current_count=len(vehicle_validation.accepted_records),
            baseline_count=baseline_count,
            volume_drop_threshold=pipeline_settings.volume_drop_threshold,
            volume_drop_policy=pipeline_settings.volume_drop_policy,
        )
        for feed_name, source_timestamp, valid_count in (
            ("system_information", system_validation.source_timestamp, 1),
            (
                "station_information",
                station_information_validation.source_timestamp,
                len(station_information_validation.accepted_records),
            ),
            (
                "station_status",
                station_status_validation.source_timestamp,
                len(station_status_validation.accepted_records),
            ),
        ):
            related_status, related_reasons = calculate_quality_status(
                now=assessed_at,
                source_timestamp=source_timestamp,
                feed_stale_minutes=pipeline_settings.feed_stale_minutes,
                current_count=valid_count,
                baseline_count=None,
                volume_drop_threshold=pipeline_settings.volume_drop_threshold,
                volume_drop_policy=pipeline_settings.volume_drop_policy,
            )
            quality_status, quality_reasons = _merge_quality(
                quality_status,
                quality_reasons,
                related_status,
                [f"{feed_name}: {reason}" for reason in related_reasons],
            )
        quality_status, quality_reasons = _assess_related_feeds(
            quality_status,
            quality_reasons,
            station_information_validation.records_received,
            len(station_information_validation.accepted_records),
            len(station_information_validation.rejected_records),
            station_status_validation.records_received,
            len(station_status_validation.accepted_records),
            len(station_status_validation.rejected_records),
        )
        transformed = transform_records(
            vehicle_validation.accepted_records,
            run_id=run_id,
            source_timestamp=vehicle_validation.source_timestamp,
            ingestion_timestamp=assessed_at,
            grid_size_degrees=pipeline_settings.grid_size_degrees,
            quality_flag=quality_status,
        )
        transformed_system = transform_system_information(
            system_validation.record,
            run_id=run_id,
            source_timestamp=system_validation.source_timestamp,
            ingestion_timestamp=assessed_at,
        )
        transformed_stations = transform_station_information(
            station_information_validation.accepted_records,
            run_id=run_id,
            source_timestamp=station_information_validation.source_timestamp,
            ingestion_timestamp=assessed_at,
        )
        transformed_station_status = transform_station_status(
            station_status_validation.accepted_records,
            run_id=run_id,
            source_timestamp=station_status_validation.source_timestamp,
            ingestion_timestamp=assessed_at,
            quality_flag=quality_status,
        )
        feed_specs: list[tuple[FeedName, datetime | None, int, int, int, list[str]]] = [
            (
                "system_information",
                system_validation.source_timestamp,
                1,
                1,
                0,
                system_validation.observed_schema_keys,
            ),
            (
                "station_information",
                station_information_validation.source_timestamp,
                station_information_validation.records_received,
                len(station_information_validation.accepted_records),
                len(station_information_validation.rejected_records),
                station_information_validation.observed_schema_keys,
            ),
            (
                "station_status",
                station_status_validation.source_timestamp,
                station_status_validation.records_received,
                len(station_status_validation.accepted_records),
                len(station_status_validation.rejected_records),
                station_status_validation.observed_schema_keys,
            ),
            (
                "free_bike_status",
                vehicle_validation.source_timestamp,
                vehicle_validation.records_received,
                len(vehicle_validation.accepted_records),
                len(vehicle_validation.rejected_records),
                vehicle_validation.observed_schema_keys,
            ),
        ]
        feed_metrics = [
            FeedRunMetric(
                feed_name=feed_name,
                source_url=feed_urls[feed_name],
                http_status=extract_results[feed_name].http_status,
                api_latency_ms=extract_results[feed_name].latency_ms,
                source_timestamp=source_timestamp,
                records_received=received,
                records_valid=valid,
                records_rejected=rejected,
                observed_schema_keys=schema_keys,
            )
            for feed_name, source_timestamp, received, valid, rejected, schema_keys in feed_specs
        ]
        records_valid = len(transformed)
        pipeline_run = PipelineRun(
            run_id=run_id,
            started_at=started_at,
            completed_at=assessed_at,
            http_status=http_status,
            records_received=records_received,
            records_valid=records_valid,
            records_rejected=records_rejected,
            api_latency_ms=api_latency_ms,
            pipeline_duration_ms=_elapsed_ms(timer_started),
            quality_status=quality_status,
            error_message="; ".join(quality_reasons) or None,
            observed_schema_keys=observed_schema_keys,
        )

        LOGGER.info(
            "run_id=%s stage=load received=%d valid=%d rejected=%d status=%s",
            run_id,
            records_received,
            records_valid,
            records_rejected,
            quality_status,
        )
        load_snapshot(
            conn,
            records=transformed,
            rejected_records=all_rejected,
            pipeline_run=pipeline_run,
            detail_retention_hours=pipeline_settings.detail_retention_hours,
            aggregate_retention_days=pipeline_settings.aggregate_retention_days,
            pipeline_run_retention_days=pipeline_settings.pipeline_run_retention_days,
            rejected_retention_days=pipeline_settings.rejected_retention_days,
            rebalance_high_threshold=pipeline_settings.rebalance_high_threshold,
            rebalance_medium_threshold=pipeline_settings.rebalance_medium_threshold,
            system_information=transformed_system,
            station_information=transformed_stations,
            station_status=transformed_station_status,
            feed_metrics=feed_metrics,
        )
        pipeline_run.completed_at = datetime.now(UTC)
        pipeline_run.pipeline_duration_ms = _elapsed_ms(timer_started)
        upsert_pipeline_run(conn, pipeline_run)
        LOGGER.info(
            "run_id=%s stage=complete status=%s duration_ms=%d",
            run_id,
            quality_status,
            pipeline_run.pipeline_duration_ms,
        )
        return 1 if quality_status == "FAILED" else 0
    except Exception as exc:
        error_message = _sanitize_error(
            exc,
            [database_settings.password] if database_settings is not None else [],
        )
        failed_run = pipeline_run or PipelineRun(
            run_id=run_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            http_status=http_status,
            records_received=records_received,
            records_valid=records_valid,
            records_rejected=records_rejected,
            api_latency_ms=api_latency_ms,
            pipeline_duration_ms=_elapsed_ms(timer_started),
            quality_status="FAILED",
            error_message=error_message,
            observed_schema_keys=observed_schema_keys,
        )
        failed_run.completed_at = datetime.now(UTC)
        failed_run.pipeline_duration_ms = _elapsed_ms(timer_started)
        failed_run.quality_status = "FAILED"
        failed_run.error_message = error_message
        if conn is not None:
            try:
                log_failed_run(conn, failed_run)
            except Exception:
                LOGGER.exception("run_id=%s stage=failure_log_failed", run_id)
        LOGGER.exception("run_id=%s stage=failed error=%s", run_id, error_message)
        return 1
    finally:
        if conn is not None:
            conn.close()


def dry_run() -> int:
    """Exercise the external feed and transformations without loading PostgreSQL."""

    run_id = "dry-run"
    try:
        settings = load_pipeline_settings()
        feed_urls = settings.feed_urls
        results = fetch_gbfs_feeds(feed_urls)
        system_validation = validate_system_information(results["system_information"].payload)
        station_information_validation = validate_station_information(
            results["station_information"].payload, settings.bbox
        )
        station_status_validation = validate_station_status(results["station_status"].payload)
        validation = validate_free_bike_status(results["free_bike_status"].payload, settings.bbox)
        assessed_at = datetime.now(UTC)
        status, reasons = calculate_quality_status(
            now=assessed_at,
            source_timestamp=validation.source_timestamp,
            feed_stale_minutes=settings.feed_stale_minutes,
            current_count=len(validation.accepted_records),
            baseline_count=None,
            volume_drop_threshold=settings.volume_drop_threshold,
            volume_drop_policy=settings.volume_drop_policy,
        )
        for feed_name, source_timestamp, valid_count in (
            ("system_information", system_validation.source_timestamp, 1),
            (
                "station_information",
                station_information_validation.source_timestamp,
                len(station_information_validation.accepted_records),
            ),
            (
                "station_status",
                station_status_validation.source_timestamp,
                len(station_status_validation.accepted_records),
            ),
        ):
            related_status, related_reasons = calculate_quality_status(
                now=assessed_at,
                source_timestamp=source_timestamp,
                feed_stale_minutes=settings.feed_stale_minutes,
                current_count=valid_count,
                baseline_count=None,
                volume_drop_threshold=settings.volume_drop_threshold,
                volume_drop_policy=settings.volume_drop_policy,
            )
            status, reasons = _merge_quality(
                status,
                reasons,
                related_status,
                [f"{feed_name}: {reason}" for reason in related_reasons],
            )
        status, reasons = _assess_related_feeds(
            status,
            reasons,
            station_information_validation.records_received,
            len(station_information_validation.accepted_records),
            len(station_information_validation.rejected_records),
            station_status_validation.records_received,
            len(station_status_validation.accepted_records),
            len(station_status_validation.rejected_records),
        )
        sample = transform_records(
            validation.accepted_records[:5],
            run_id=run_id,
            source_timestamp=validation.source_timestamp,
            ingestion_timestamp=assessed_at,
            grid_size_degrees=settings.grid_size_degrees,
            quality_flag=status,
        )
        system_sample = transform_system_information(
            system_validation.record,
            run_id=run_id,
            source_timestamp=system_validation.source_timestamp,
            ingestion_timestamp=assessed_at,
        )
        station_sample = transform_station_information(
            station_information_validation.accepted_records[:5],
            run_id=run_id,
            source_timestamp=station_information_validation.source_timestamp,
            ingestion_timestamp=assessed_at,
        )
        status_sample = transform_station_status(
            station_status_validation.accepted_records[:5],
            run_id=run_id,
            source_timestamp=station_status_validation.source_timestamp,
            ingestion_timestamp=assessed_at,
            quality_flag=status,
        )
        LOGGER.info(
            "run_id=%s status=%s system=%s stations=%d station_statuses=%d "
            "vehicles_received=%d vehicles_valid=%d rejected=%d sample_grids=%s "
            "sample_station_ids=%s sample_status_ids=%s reasons=%s",
            run_id,
            status,
            system_sample.system_id,
            len(station_information_validation.accepted_records),
            len(station_status_validation.accepted_records),
            validation.records_received,
            len(validation.accepted_records),
            len(validation.rejected_records)
            + len(station_information_validation.rejected_records)
            + len(station_status_validation.rejected_records),
            [record.grid_id for record in sample],
            [record.station_id for record in station_sample],
            [record.station_id for record in status_sample],
            reasons,
        )
        return 1 if status == "FAILED" else 0
    except Exception as exc:
        LOGGER.exception("run_id=%s stage=failed error=%s", run_id, _sanitize_error(exc, []))
        return 1


def _assess_related_feeds(
    status: QualityStatus,
    reasons: list[str],
    station_information_received: int,
    station_information_valid: int,
    station_information_rejected: int,
    station_status_received: int,
    station_status_valid: int,
    station_status_rejected: int,
) -> tuple[QualityStatus, list[str]]:
    related_reasons = list(reasons)
    if station_information_received == 0 or station_information_valid == 0:
        status = "FAILED"
        related_reasons.append("Station information feed produced zero valid records")
    if station_status_received == 0 or station_status_valid == 0:
        status = "FAILED"
        related_reasons.append("Station status feed produced zero valid records")
    rejected = station_information_rejected + station_status_rejected
    if rejected and status == "SUCCESS":
        status = "WARNING"
    if rejected:
        related_reasons.append(f"Related station feeds rejected {rejected} records")
    return status, related_reasons


def _merge_quality(
    current_status: QualityStatus,
    current_reasons: list[str],
    related_status: QualityStatus,
    related_reasons: list[str],
) -> tuple[QualityStatus, list[str]]:
    severity: dict[QualityStatus, int] = {"SUCCESS": 0, "WARNING": 1, "FAILED": 2}
    status = (
        related_status if severity[related_status] > severity[current_status] else current_status
    )
    return status, [*current_reasons, *related_reasons]


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest all configured GBFS fleet feeds.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, validate, and transform without connecting to PostgreSQL.",
    )
    args = parser.parse_args()
    _configure_logging()
    return dry_run() if args.dry_run else run()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1_000)


def _sanitize_error(exc: Exception, secrets: list[str]) -> str:
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:MAX_ERROR_LENGTH]


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        force=True,
    )
