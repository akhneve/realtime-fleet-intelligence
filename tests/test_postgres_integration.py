import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from fleet_intelligence.load import load_snapshot
from fleet_intelligence.models import (
    FeedRunMetric,
    PipelineRun,
    StationInformationRecord,
    StationStatusSnapshot,
    SystemInformationRecord,
    VehicleSnapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def apply_migrations(conn: psycopg.Connection[object]) -> None:
    # Supabase supplies these roles; the portable CI database creates stand-ins.
    conn.execute(
        """
        do $$ begin
            if not exists (select 1 from pg_roles where rolname = 'anon') then
                create role anon;
            end if;
            if not exists (select 1 from pg_roles where rolname = 'authenticated') then
                create role authenticated;
            end if;
        end $$
        """
    )
    for path in sorted((PROJECT_ROOT / "sql").glob("*.sql")):
        conn.execute(path.read_text(encoding="utf-8"))


def test_migrations_loading_views_and_rollback() -> None:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        apply_migrations(conn)
        apply_migrations(conn)

        columns = [
            row[0]
            for row in conn.execute(
                """
                select column_name from information_schema.columns
                where table_schema = 'public' and table_name = 'vw_rebalancing_priority'
                order by ordinal_position
                """
            ).fetchall()
        ]
        assert columns[:7] == [
            "grid_id",
            "current_available_vehicles",
            "historical_typical_available_vehicles",
            "supply_gap",
            "availability_rate",
            "priority",
            "interpretation",
        ]
        assert columns[-2:] == ["baseline_sample_count", "baseline_ready"]

        run_id = "00000000-0000-0000-0000-000000000001"
        timestamp = datetime.now(UTC)
        record = VehicleSnapshot(
            "vehicle-1",
            "scooter",
            47.61,
            -122.33,
            False,
            False,
            True,
            "GRID_1",
            run_id,
            timestamp,
            timestamp,
            timestamp,
            "SUCCESS",
        )
        run = PipelineRun(
            run_id,
            timestamp,
            timestamp,
            200,
            1,
            1,
            0,
            10,
            20,
            "SUCCESS",
            None,
            ["bike_id"],
        )
        load_snapshot(
            conn,
            records=[record],
            rejected_records=[],
            pipeline_run=run,
            detail_retention_hours=24,
            aggregate_retention_days=35,
            pipeline_run_retention_days=90,
            rejected_retention_days=14,
            rebalance_high_threshold=10,
            rebalance_medium_threshold=5,
            system_information=SystemInformationRecord(
                "lime_seattle",
                "Lime Seattle",
                "en",
                "America/Los_Angeles",
                None,
                "Lime",
                timestamp,
                timestamp,
                run_id,
            ),
            station_information=[
                StationInformationRecord(
                    "seattle",
                    "Seattle",
                    None,
                    47.61,
                    -122.33,
                    None,
                    None,
                    timestamp,
                    timestamp,
                    run_id,
                )
            ],
            station_status=[
                StationStatusSnapshot(
                    "seattle",
                    1,
                    999999,
                    True,
                    True,
                    True,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    run_id,
                    "SUCCESS",
                )
            ],
            feed_metrics=[
                FeedRunMetric(
                    feed_name,
                    f"https://example.test/{feed_name}",
                    200,
                    10,
                    timestamp,
                    1,
                    1,
                    0,
                    [],
                )
                for feed_name in (
                    "system_information",
                    "station_information",
                    "station_status",
                    "free_bike_status",
                )
            ],
        )
        assert conn.execute("select count(*) from current_vehicle_state").fetchone()[0] == 1
        assert conn.execute("select count(*) from vw_current_grid_supply").fetchone()[0] == 1
        assert conn.execute("select count(*) from system_information").fetchone()[0] == 1
        assert conn.execute("select count(*) from vw_current_station_supply").fetchone()[0] == 1
        assert conn.execute("select count(*) from fact_station_status_snapshot").fetchone()[0] == 1
        assert conn.execute("select count(*) from feed_run_metrics").fetchone()[0] == 4
        enriched = conn.execute(
            """
            select
                neighborhood_name,
                zip_code,
                council_district_name,
                grid_id,
                source_timestamp,
                ingestion_timestamp
            from vw_vehicle_geography_enriched
            where vehicle_id = 'vehicle-1'
            """
        ).fetchone()
        assert enriched is not None
        assert enriched[:4] == (None, None, None, None)
        assert enriched[4:] == (timestamp, timestamp)

        bad_run = PipelineRun(
            "00000000-0000-0000-0000-000000000002",
            timestamp,
            timestamp,
            200,
            99,
            1,
            0,
            10,
            20,
            "SUCCESS",
            None,
            [],
        )
        bad_record = VehicleSnapshot(
            "vehicle-2",
            None,
            47.62,
            -122.34,
            False,
            False,
            True,
            "GRID_2",
            bad_run.run_id,
            timestamp,
            timestamp,
            timestamp,
            "SUCCESS",
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            load_snapshot(
                conn,
                records=[bad_record],
                rejected_records=[],
                pipeline_run=bad_run,
                detail_retention_hours=24,
                aggregate_retention_days=35,
                pipeline_run_retention_days=90,
                rejected_retention_days=14,
                rebalance_high_threshold=10,
                rebalance_medium_threshold=5,
            )
        assert (
            conn.execute(
                "select count(*) from current_vehicle_state where vehicle_id = 'vehicle-2'"
            ).fetchone()[0]
            == 0
        )

        ingest_can_write = conn.execute(
            "select has_table_privilege('fleet_ingest', 'pipeline_runs', 'insert')"
        ).fetchone()[0]
        reporting_can_read = conn.execute(
            "select has_table_privilege('fleet_reporting', 'vw_pipeline_health', 'select')"
        ).fetchone()[0]
        reporting_can_write = conn.execute(
            "select has_table_privilege('fleet_reporting', 'pipeline_runs', 'insert')"
        ).fetchone()[0]
        assert ingest_can_write and reporting_can_read and not reporting_can_write
