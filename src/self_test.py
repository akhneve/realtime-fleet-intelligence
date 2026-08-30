"""Built-in self-test runner for the fleet ETL codebase."""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import os
import traceback
from typing import Callable

from .config import load_settings
from .extract import fetch_gbfs
from .load import connect, get_recent_baseline_count
from .metrics import build_grid_15min_rows, calculate_quality_status
from .transform import assign_grid_id, derive_available_flag, transform_records
from .validate import validate_payload, validate_records


TestFn = Callable[[], None]


def _pass(name: str) -> None:
    print(f"PASS  {name}")


def _fail(name: str, exc: Exception, verbose: bool) -> None:
    print(f"FAIL  {name}: {exc}")
    if verbose:
        traceback.print_exc()


def _run_test(name: str, test_fn: TestFn, verbose: bool) -> bool:
    # What: wraps each self-test in identical PASS/FAIL reporting. Why: one broken area should not hide the status of the rest of the codebase.
    try:
        test_fn()
    except Exception as exc:
        _fail(name, exc, verbose)
        return False
    _pass(name)
    return True


def check_config_loads_from_dotenv() -> None:
    # What: loads project settings from .env through the real config path. Why: local Supabase credentials should be picked up without manual shell exports.
    settings = load_settings()
    assert settings.db_host.strip(), "DB_HOST is empty"
    assert settings.db_name.strip(), "DB_NAME is empty"
    assert settings.db_user.strip(), "DB_USER is empty"
    assert settings.db_password.strip(), "DB_PASSWORD is empty"
    assert settings.db_port > 0, "DB_PORT must be positive"


def check_validation_logic() -> None:
    # What: exercises valid, invalid, duplicate, missing-ID, and malformed GBFS records. Why: bad records must be quarantined predictably.
    accepted, rejected, observed_keys = validate_records(
        [
            {"bike_id": "ok-1", "lat": 47.61, "lon": -122.33, "is_reserved": False, "is_disabled": False},
            {"bike_id": "bad-lat", "lat": 91, "lon": -122.33},
            {"bike_id": "dupe", "lat": 47.62, "lon": -122.34},
            {"bike_id": "dupe", "lat": 47.63, "lon": -122.35},
            {"lat": 47.64, "lon": -122.36},
            "malformed",
        ]
    )
    assert len(accepted) == 1, f"expected 1 accepted record, got {len(accepted)}"
    assert len(rejected) == 5, f"expected 5 rejected records, got {len(rejected)}"
    assert "bike_id" in observed_keys, "schema keys were not captured"
    assert {record.reason_code for record in rejected} == {"INVALID_LATITUDE", "DUPLICATE_ID", "MISSING_ID", "MALFORMED_RECORD"}


def check_transform_logic() -> None:
    # What: checks availability, grid IDs, and metadata enrichment. Why: the loader and dashboard depend on these derived fields already existing.
    timestamp = datetime(2026, 8, 30, 12, 3, tzinfo=timezone.utc)
    transformed = transform_records(
        [
            {
                "vehicle_id": "bike-1",
                "vehicle_type_id": "scooter",
                "latitude": 47.61,
                "longitude": -122.33,
                "is_reserved": False,
                "is_disabled": True,
                "raw_payload": {"bike_id": "bike-1"},
            }
        ],
        run_id="self-test",
        source_timestamp=timestamp,
        ingestion_timestamp=timestamp,
        source_feed="self_test_feed",
        grid_size_degrees=0.01,
        quality_flag="SUCCESS",
    )
    row = transformed[0]
    assert row["available_flag"] is False, "disabled vehicle should be unavailable"
    assert row["grid_id"] == assign_grid_id(47.61, -122.33, 0.01), "grid assignment is not deterministic"
    assert row["run_id"] == "self-test", "run_id metadata missing"


def check_metrics_logic() -> None:
    # What: checks 15-minute aggregation and volume-drop detection. Why: these are the main QA and long-term reporting behaviors.
    timestamp = datetime(2026, 8, 30, 12, 14, 59, tzinfo=timezone.utc)
    rows = build_grid_15min_rows(
        [
            {"snapshot_timestamp": timestamp, "grid_id": "GRID_1", "available_flag": True, "run_id": "self-test"},
            {"snapshot_timestamp": timestamp, "grid_id": "GRID_1", "available_flag": False, "run_id": "self-test"},
        ]
    )
    assert rows[0]["total_vehicles"] == 2, "aggregate total count is wrong"
    assert rows[0]["available_vehicles"] == 1, "aggregate available count is wrong"
    status, reasons = calculate_quality_status(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        source_timestamp=None,
        feed_stale_minutes=None,
        current_count=25,
        baseline_count=100,
        volume_drop_threshold=0.50,
        volume_drop_policy="WARNING",
    )
    assert status == "WARNING", "75% volume drop should trigger WARNING"
    assert reasons, "volume-drop warning should include a reason"
    assert derive_available_flag(False, False) is True, "available flag logic failed"


def check_live_extract_and_payload_validation() -> None:
    # What: fetches the real Lime feed and validates its GBFS shape. Why: this proves the external API still matches pipeline expectations.
    settings = load_settings()
    result = fetch_gbfs(settings.gbfs_url, timeout_seconds=30, retries=1)
    validation = validate_payload(result.payload, bbox=settings.bbox)
    assert result.http_status == 200, f"expected HTTP 200, got {result.http_status}"
    assert validation.records_received > 0, "live feed returned zero vehicles"
    assert validation.accepted_records, "live feed produced zero valid vehicles"
    print(f"      live records received: {validation.records_received}")
    print(f"      live records valid: {len(validation.accepted_records)}")
    print(f"      live records rejected: {len(validation.rejected_records)}")


def check_supabase_connection() -> None:
    # What: opens a real database connection and runs a simple query. Why: this proves the .env Supabase/Postgres settings work before running ingestion.
    settings = load_settings()
    conn = connect(settings)
    try:
        with conn.cursor() as cur:
            cur.execute("select current_database(), current_user, now()")
            row = cur.fetchone()
            assert row is not None, "database did not return a row"
            print(f"      connected database/user: {row[0]} / {row[1]}")
            required_relations = [
                "current_vehicle_state",
                "fact_vehicle_snapshot",
                "fact_grid_15min",
                "rejected_records",
                "pipeline_runs",
                "reporting_thresholds",
            ]
            cur.execute(
                "select relation_name from unnest(%s::text[]) relation_name where to_regclass('public.' || relation_name) is null",
                (required_relations,),
            )
            missing_relations = [missing[0] for missing in cur.fetchall()]
            assert not missing_relations, f"database schema is missing: {', '.join(missing_relations)}"
        baseline = get_recent_baseline_count(conn)
        print(f"      recent baseline count: {baseline}")
    finally:
        conn.close()


def check_dry_pipeline_without_database_write() -> None:
    # What: runs extract -> validate -> QA -> transform on live data without loading tables. Why: this tests the pipeline path without mutating the database.
    settings = load_settings()
    result = fetch_gbfs(settings.gbfs_url, timeout_seconds=30, retries=1)
    validation = validate_payload(result.payload, bbox=settings.bbox)
    status, reasons = calculate_quality_status(
        now=datetime.now(timezone.utc),
        source_timestamp=validation.source_timestamp,
        feed_stale_minutes=settings.feed_stale_minutes,
        current_count=len(validation.accepted_records),
        baseline_count=None,
        volume_drop_threshold=settings.volume_drop_threshold,
        volume_drop_policy=settings.volume_drop_policy,
    )
    transformed = transform_records(
        validation.accepted_records[:10],
        run_id="self-test-dry-run",
        source_timestamp=validation.source_timestamp,
        ingestion_timestamp=datetime.now(timezone.utc),
        source_feed=settings.source_feed,
        grid_size_degrees=settings.grid_size_degrees,
        quality_flag=status,
    )
    assert transformed, "dry pipeline did not produce transformed rows"
    assert "grid_id" in transformed[0], "transformed row is missing grid_id"
    print(f"      dry quality status: {status}")
    print(f"      dry quality reasons: {reasons}")


def run_self_tests(skip_live: bool = False, skip_db: bool = False, verbose: bool = False) -> int:
    # What: runs all built-in checks from one command. Why: manual one-liners are annoying and easy to mistype.
    tests: list[tuple[str, TestFn]] = [
        ("config loads from .env", check_config_loads_from_dotenv),
        ("validation logic", check_validation_logic),
        ("transform logic", check_transform_logic),
        ("metrics logic", check_metrics_logic),
    ]
    if not skip_live:
        tests.append(("live extract and payload validation", check_live_extract_and_payload_validation))
        tests.append(("dry pipeline without database write", check_dry_pipeline_without_database_write))
    if not skip_db:
        tests.append(("Supabase/Postgres connection", check_supabase_connection))

    print("Running built-in fleet ETL self-tests")
    print(f"Using .env file automatically from: {os.getcwd()}")
    passed = 0
    for name, test_fn in tests:
        if _run_test(name, test_fn, verbose):
            passed += 1

    total = len(tests)
    print(f"\nSelf-test summary: {passed}/{total} passed")
    return 0 if passed == total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run built-in fleet ETL self-tests.")
    parser.add_argument("--skip-live", action="store_true", help="Skip tests that call the live Lime GBFS API.")
    parser.add_argument("--skip-db", action="store_true", help="Skip the Supabase/Postgres connection test.")
    parser.add_argument("--verbose", action="store_true", help="Print full tracebacks for failures.")
    args = parser.parse_args()
    return run_self_tests(skip_live=args.skip_live, skip_db=args.skip_db, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
