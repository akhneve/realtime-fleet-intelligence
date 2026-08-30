"""Entrypoint for the scheduled GBFS ingestion job."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
import time
import uuid

from .config import load_settings
from .extract import fetch_gbfs
from .load import connect, get_recent_baseline_count, load_snapshot, log_failed_run, upsert_pipeline_run
from .metrics import calculate_quality_status
from .transform import transform_records
from .validate import validate_payload


def run() -> int:
    # What: orchestrates one production ingestion run. Why: GitHub Actions needs one command that performs the full ETL contract.
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)  # What: records run start in UTC. Why: pipeline health needs consistent timestamps.
    timer_started = time.perf_counter()  # What: starts a monotonic timer. Why: runtime metrics should not be affected by system clock changes.
    conn = None  # What: defers DB connection assignment. Why: finally/except blocks can safely run even if startup fails.
    pipeline_run = None  # What: retains collected metrics across failure handling. Why: late failures should not erase already known HTTP and record counts.

    try:
        print(f"Starting fleet ingestion (run_id={run_id})", flush=True)
        settings = load_settings()  # What: loads env-backed configuration. Why: secrets and thresholds stay outside source code.
        print("Connecting to PostgreSQL...", flush=True)
        conn = connect(settings)  # What: opens the database connection. Why: later load steps need one transaction-capable connection.
        print("Connected. Reading recent pipeline baseline...", flush=True)
        baseline_count = get_recent_baseline_count(conn)  # What: fetches recent normal volume. Why: current snapshot count needs context.
        print("Fetching the live GBFS feed...", flush=True)
        extract_result = fetch_gbfs(settings.gbfs_url)  # What: downloads live GBFS data. Why: this is the source snapshot for the run.
        print("Validating and transforming source records...", flush=True)
        validation = validate_payload(extract_result.payload, bbox=settings.bbox)  # What: splits valid and rejected source records. Why: only clean rows should reach operational tables.
        completed_at = datetime.now(timezone.utc)
        quality_status, quality_reasons = calculate_quality_status(
            # What: evaluates stale-feed and volume-drop warnings/failures. Why: suspicious snapshots should be marked before loading.
            now=completed_at,
            source_timestamp=validation.source_timestamp,
            feed_stale_minutes=settings.feed_stale_minutes,
            current_count=len(validation.accepted_records),
            baseline_count=baseline_count,
            volume_drop_threshold=settings.volume_drop_threshold,
            volume_drop_policy=settings.volume_drop_policy,
        )
        transformed = transform_records(
            # What: adds availability, grid IDs, timestamps, and lineage. Why: database rows should already be reporting-ready.
            validation.accepted_records,
            run_id=run_id,
            source_timestamp=validation.source_timestamp,
            ingestion_timestamp=completed_at,
            source_feed=settings.source_feed,
            grid_size_degrees=settings.grid_size_degrees,
            quality_flag=quality_status,
        )
        pipeline_run = {
            # What: builds the observability payload for pipeline_runs. Why: dashboard health metrics should come from the same transaction as the data.
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "http_status": extract_result.http_status,
            "records_received": validation.records_received,
            "records_valid": len(transformed),
            "records_rejected": len(validation.rejected_records),
            "api_latency_ms": extract_result.latency_ms,
            "pipeline_duration_ms": int((time.perf_counter() - timer_started) * 1000),
            "quality_status": quality_status,
            "error_message": "; ".join(quality_reasons) or None,
            "observed_schema_keys": validation.observed_schema_keys,
        }
        print(
            f"Loading {len(transformed)} valid and {len(validation.rejected_records)} rejected records...",
            flush=True,
        )
        load_snapshot(
            conn,
            records=transformed,
            rejected_records=validation.rejected_records,
            pipeline_run=pipeline_run,
            detail_retention_days=settings.detail_retention_days,
            rejected_retention_days=settings.rejected_retention_days,
            rebalance_high_threshold=settings.rebalance_high_threshold,
            rebalance_medium_threshold=settings.rebalance_medium_threshold,
        )  # What: atomically writes the snapshot, thresholds, run record, and retention cleanup. Why: avoids partial reporting state.
        pipeline_run["completed_at"] = datetime.now(timezone.utc)
        pipeline_run["pipeline_duration_ms"] = int((time.perf_counter() - timer_started) * 1000)
        upsert_pipeline_run(conn, pipeline_run)  # What: refreshes final timing after the write transaction commits. Why: duration should include database work.
        print(
            "Ingestion completed: "
            f"status={quality_status}, received={validation.records_received}, "
            f"valid={len(transformed)}, rejected={len(validation.rejected_records)}, "
            f"duration_ms={pipeline_run['pipeline_duration_ms']}, run_id={run_id}",
            flush=True,
        )
        return 1 if quality_status == "FAILED" else 0
    except Exception as exc:
        # What: converts unexpected failures into a FAILED pipeline run when possible. Why: failed jobs should be visible in observability tables and GitHub Actions.
        completed_at = datetime.now(timezone.utc)
        failed_run = dict(pipeline_run) if pipeline_run is not None else {
            "run_id": run_id,
            "started_at": started_at,
            "http_status": None,
            "records_received": 0,
            "records_valid": 0,
            "records_rejected": 0,
            "api_latency_ms": None,
            "observed_schema_keys": [],
        }
        failed_run.update(
            completed_at=completed_at,
            pipeline_duration_ms=int((time.perf_counter() - timer_started) * 1000),
            quality_status="FAILED",
            error_message=str(exc),
        )
        try:
            log_failed_run(conn, failed_run)
        except Exception:
            # What: suppresses secondary logging failures. Why: the original failure should still produce a clean non-zero exit.
            pass
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            # What: closes the DB connection. Why: scheduled jobs should release database resources promptly.
            conn.close()


def dry_run() -> int:
    """Run with: FLEET_DRY_RUN=1 python -m src.main"""
    # What: exercises extract, validate, QA, and transform without writing to PostgreSQL. Why: you can test live pipeline logic before database setup.
    started_at = datetime.now(timezone.utc)
    try:
        # What: fills fake DB env vars only for dry-run config loading. Why: dry-run should not require real secrets because it never connects.
        os.environ.setdefault("DB_HOST", "localhost")
        os.environ.setdefault("DB_NAME", "fleet")
        os.environ.setdefault("DB_USER", "fleet_user")
        os.environ.setdefault("DB_PASSWORD", "not-a-real-password")
        settings = load_settings()
        extract_result = fetch_gbfs(settings.gbfs_url)  # What: confirms the live feed is reachable. Why: this validates the first external dependency.
        validation = validate_payload(extract_result.payload, bbox=settings.bbox)  # What: runs real validation against live data. Why: schema changes should show up before scheduled runs.
        quality_status, quality_reasons = calculate_quality_status(
            now=datetime.now(timezone.utc),
            source_timestamp=validation.source_timestamp,
            feed_stale_minutes=settings.feed_stale_minutes,
            current_count=len(validation.accepted_records),
            baseline_count=None,
            volume_drop_threshold=settings.volume_drop_threshold,
            volume_drop_policy=settings.volume_drop_policy,
        )
        transformed = transform_records(
            # What: transforms only a small sample for printing. Why: dry-run output stays readable even when the feed has thousands of vehicles.
            validation.accepted_records[:5],
            run_id="dry-run",
            source_timestamp=validation.source_timestamp,
            ingestion_timestamp=started_at,
            source_feed=settings.source_feed,
            grid_size_degrees=settings.grid_size_degrees,
            quality_flag=quality_status,
        )
        print("Main dry-run smoke test passed")
        print(f"HTTP status: {extract_result.http_status}")
        print(f"Records received: {validation.records_received}")
        print(f"Records valid: {len(validation.accepted_records)}")
        print(f"Records rejected: {len(validation.rejected_records)}")
        print(f"Quality status: {quality_status}")
        print(f"Quality reasons: {quality_reasons}")
        print(f"Sample transformed rows: {transformed[:2]}")
        return 0
    except Exception as exc:
        print(f"Main dry-run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # What: switches between dry-run and production modes using an env flag. Why: one module can support both local testing and scheduled ingestion.
    if os.getenv("FLEET_DRY_RUN") == "1":
        raise SystemExit(dry_run())
    raise SystemExit(run())
