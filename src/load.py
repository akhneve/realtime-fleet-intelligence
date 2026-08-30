"""Transactional PostgreSQL loading for fleet snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    # What: imports the PostgreSQL driver when installed. Why: most local smoke tests should still import even before DB dependencies are visible.
    import psycopg
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:
    psycopg = None
    Jsonb = None

from .config import Settings
from .metrics import build_grid_15min_rows
from .validate import RejectedRecord


def connect(settings: Settings) -> Any:
    # What: opens a PostgreSQL connection from environment-backed settings. Why: all database writes should use configured secrets, not hard-coded credentials.
    if psycopg is None:
        raise RuntimeError("psycopg is required for database loading. Install dependencies with: pip install -r requirements.txt")
    return psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        connect_timeout=10,  # What: bounds initial connection time. Why: a blocked database should fail visibly instead of appearing to hang forever.
        # What: leaves standalone reads outside implicit transactions. Why: each write path already uses conn.transaction(), and an
        # earlier baseline SELECT must not create an outer transaction that turns those write blocks into savepoints rolled back on close.
        autocommit=True,
    )


def get_recent_baseline_count(conn: Any) -> int | None:
    # What: reads the average valid record count from recent successful/warning runs. Why: volume anomaly detection needs a recent baseline.
    with conn.cursor() as cur:
        cur.execute(
            """
            select avg(records_valid)::int
            from (
                select records_valid
                from pipeline_runs
                where quality_status in ('SUCCESS', 'WARNING')
                  and records_valid > 0
                order by started_at desc
                limit 12
            ) recent
            """
        )
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def load_snapshot(
    conn: Any,
    *,
    records: list[dict[str, Any]],
    rejected_records: list[RejectedRecord],
    pipeline_run: dict[str, Any],
    detail_retention_days: int,
    rejected_retention_days: int,
    rebalance_high_threshold: int,
    rebalance_medium_threshold: int,
) -> None:
    # What: writes accepted records, rejected records, aggregates, and run metadata together. Why: the dashboard should never see a half-loaded snapshot.
    aggregates = build_grid_15min_rows(records)  # What: prepares compact grid metrics before opening the transaction. Why: DB time stays focused on writes.
    with conn.transaction():
        # What: transaction boundary for all reporting tables. Why: any failure rolls the whole snapshot back.
        _insert_vehicle_snapshots(conn, records)
        if pipeline_run["quality_status"] != "FAILED":
            # What: skips current-state overwrite on failed-quality snapshots. Why: a severe anomaly should not blindly replace usable live state.
            _upsert_current_state(conn, records)
            _delete_stale_current_state(conn, pipeline_run["run_id"])
            _replace_grid_aggregates(conn, aggregates)
        _insert_rejected_records(conn, pipeline_run["run_id"], rejected_records)
        _upsert_reporting_thresholds(conn, rebalance_high_threshold, rebalance_medium_threshold)
        _apply_retention(conn, detail_retention_days, rejected_retention_days)
        _insert_pipeline_run(conn, pipeline_run)


def log_failed_run(conn: Any, pipeline_run: dict[str, Any]) -> None:
    # What: records a failed pipeline attempt. Why: failures should still be visible in pipeline health reporting when the database is reachable.
    with conn.transaction():
        _insert_pipeline_run(conn, pipeline_run)


def upsert_pipeline_run(conn: Any, pipeline_run: dict[str, Any]) -> None:
    # What: persists final run timing and status after the snapshot transaction commits. Why: operational duration must include database work.
    with conn.transaction():
        _insert_pipeline_run(conn, pipeline_run)


def apply_retention(conn: Any, detail_retention_days: int, rejected_retention_days: int) -> None:
    # What: deletes old detailed rows and old rejected payloads. Why: vehicle-level history grows quickly and must fit MVP database limits.
    with conn.transaction():
        _apply_retention(conn, detail_retention_days, rejected_retention_days)


def _apply_retention(conn: Any, detail_retention_days: int, rejected_retention_days: int) -> None:
    # What: removes expired detail, aggregates, run logs, and rejected payloads inside the caller's transaction. Why: all historical products obey bounded retention atomically.
    with conn.cursor() as cur:
        cur.execute(
            "delete from fact_vehicle_snapshot where snapshot_timestamp < now() - (%s || ' days')::interval",
            (detail_retention_days,),
        )
        cur.execute(
            "delete from fact_grid_15min where bucket_timestamp < now() - (%s || ' days')::interval",
            (detail_retention_days,),
        )
        cur.execute(
            "delete from rejected_records where rejected_at < now() - (%s || ' days')::interval",
            (rejected_retention_days,),
        )
        cur.execute(
            "delete from pipeline_runs where started_at < now() - (%s || ' days')::interval",
            (detail_retention_days,),
        )


def _insert_vehicle_snapshots(conn: Any, records: list[dict[str, Any]]) -> None:
    # What: inserts vehicle-level history for this run. Why: recent detail is useful for debugging and short-term analysis.
    if not records:
        return
    rows = [
        # What: converts dictionaries into DB parameter tuples. Why: parameterized executemany is safer and faster than building SQL strings.
        (
            record["snapshot_timestamp"],
            record["vehicle_id"],
            record["latitude"],
            record["longitude"],
            record["available_flag"],
            record["grid_id"],
            record["run_id"],
            record["quality_flag"],
        )
        for record in records
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into fact_vehicle_snapshot (
                snapshot_timestamp, vehicle_id, latitude, longitude,
                available_flag, grid_id, run_id, quality_flag
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id, vehicle_id) do nothing
            -- What: ignores duplicate rows for the same run/vehicle. Why: rerunning a job should be idempotent.
            """,
            rows,
        )


def _upsert_current_state(conn: Any, records: list[dict[str, Any]]) -> None:
    # What: updates the live one-row-per-vehicle table. Why: operational dashboards need the latest state without scanning history.
    if not records:
        return
    rows = [
        # What: extracts only columns needed by current_vehicle_state. Why: keeping this table narrow makes live DirectQuery faster.
        (
            record["vehicle_id"],
            record.get("vehicle_type_id"),
            record["latitude"],
            record["longitude"],
            record.get("is_reserved"),
            record.get("is_disabled"),
            record["available_flag"],
            record["grid_id"],
            record["source_timestamp"],
            record["ingestion_timestamp"],
            record["run_id"],
        )
        for record in records
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into current_vehicle_state (
                vehicle_id, vehicle_type_id, latitude, longitude, is_reserved,
                is_disabled, available_flag, grid_id, source_timestamp,
                ingestion_timestamp, run_id
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (vehicle_id) do update set
                -- What: replaces existing state for the same vehicle. Why: current_vehicle_state should always reflect the latest accepted snapshot.
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
            """,
            rows,
        )


def _delete_stale_current_state(conn: Any, run_id: str) -> None:
    # What: removes vehicles absent from the accepted full snapshot. Why: current_vehicle_state must represent one snapshot, not accumulate old vehicles.
    with conn.cursor() as cur:
        cur.execute("delete from current_vehicle_state where run_id <> %s", (run_id,))


def _insert_rejected_records(conn: Any, run_id: str, rejected_records: list[RejectedRecord]) -> None:
    # What: writes invalid records to the quarantine table. Why: bad source data should be inspectable, not silently discarded.
    if not rejected_records:
        return
    if Jsonb is None:
        raise RuntimeError("psycopg is required for JSONB database loading. Install dependencies with: pip install -r requirements.txt")
    rejected_at = datetime.now(timezone.utc)  # What: one rejection timestamp for this batch. Why: consistent batch timing simplifies QA trends.
    rows = [
        # What: wraps raw dictionaries as PostgreSQL JSONB values. Why: rejected source payloads need to stay queryable in Postgres.
        (
            run_id,
            rejected.record_key,
            Jsonb(rejected.raw_payload),
            rejected.reason_code,
            rejected.reason_detail,
            rejected_at,
        )
        for rejected in rejected_records
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into rejected_records (
                run_id, record_key, raw_payload, reason_code, reason_detail, rejected_at
            )
            values (%s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def _replace_grid_aggregates(conn: Any, aggregates: list[dict[str, Any]]) -> None:
    # What: replaces every affected 15-minute bucket before inserting its grids. Why: a bucket must never mix grids from different snapshots.
    if not aggregates:
        return
    rows = [
        # What: converts aggregate dictionaries into DB parameter tuples. Why: executemany can load all grid buckets efficiently.
        (
            row["bucket_timestamp"],
            row["grid_id"],
            row["total_vehicles"],
            row["available_vehicles"],
            row["unavailable_vehicles"],
            row["run_id"],
        )
        for row in aggregates
    ]
    with conn.cursor() as cur:
        bucket_timestamps = sorted({row["bucket_timestamp"] for row in aggregates})
        cur.executemany(
            "delete from fact_grid_15min where bucket_timestamp = %s",
            [(bucket_timestamp,) for bucket_timestamp in bucket_timestamps],
        )
        cur.executemany(
            """
            insert into fact_grid_15min (
                bucket_timestamp, grid_id, total_vehicles, available_vehicles,
                unavailable_vehicles, run_id
            )
            values (%s, %s, %s, %s, %s, %s)
            on conflict (bucket_timestamp, grid_id) do update set
                -- What: updates an existing bucket/grid aggregate. Why: repeated runs for the same bucket should converge to one row.
                total_vehicles = excluded.total_vehicles,
                available_vehicles = excluded.available_vehicles,
                unavailable_vehicles = excluded.unavailable_vehicles,
                run_id = excluded.run_id
            """,
            rows,
        )


def _upsert_reporting_thresholds(conn: Any, high_threshold: int, medium_threshold: int) -> None:
    # What: synchronizes environment-backed rebalancing settings into the SQL configuration table. Why: configured values must affect reporting views.
    with conn.cursor() as cur:
        cur.executemany(
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


def _insert_pipeline_run(conn: Any, pipeline_run: dict[str, Any]) -> None:
    # What: writes the pipeline observability record. Why: operators need run status, latency, counts, and errors in the reporting layer.
    if Jsonb is None:
        raise RuntimeError("psycopg is required for JSONB database loading. Install dependencies with: pip install -r requirements.txt")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into pipeline_runs (
                run_id, started_at, completed_at, http_status, records_received,
                records_valid, records_rejected, api_latency_ms,
                pipeline_duration_ms, quality_status, error_message,
                observed_schema_keys
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (run_id) do update set
                -- What: updates the same run record if it is written again. Why: failed-run logging and retries should remain idempotent.
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
                pipeline_run["run_id"],
                pipeline_run["started_at"],
                pipeline_run["completed_at"],
                pipeline_run.get("http_status"),
                pipeline_run["records_received"],
                pipeline_run["records_valid"],
                pipeline_run["records_rejected"],
                pipeline_run.get("api_latency_ms"),
                pipeline_run["pipeline_duration_ms"],
                pipeline_run["quality_status"],
                pipeline_run.get("error_message"),
                Jsonb(pipeline_run.get("observed_schema_keys", [])),
            ),
        )


def _smoke_test() -> None:
    """Run with: python -m src.load"""
    # What: confirms the load module can be executed directly. Why: full DB tests need credentials, but this still documents the next verification step.
    print("Load smoke test passed")
    print("This module needs a live PostgreSQL connection for full verification.")
    print("Run SQL files in order, configure .env values, then run: python -m src.main")
    print("Core functions available: connect, load_snapshot, log_failed_run, apply_retention")


if __name__ == "__main__":
    _smoke_test()
