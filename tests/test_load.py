from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import fleet_intelligence.load as load_module
from fleet_intelligence.config import DatabaseSettings
from fleet_intelligence.models import (
    FeedRunMetric,
    PipelineRun,
    RejectedRecord,
    StationInformationRecord,
    StationStatusSnapshot,
    SystemInformationRecord,
    VehicleSnapshot,
)

RUN_ID = "00000000-0000-0000-0000-000000000001"
TIMESTAMP = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class Context:
    def __enter__(self) -> Context:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class RecordingCopy(Context):
    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []

    def write_row(self, row: tuple[object, ...]) -> None:
        self.rows.append(row)


class RecordingCursor(Context):
    def __init__(self, fetchone_value: tuple[object, ...] | None = None) -> None:
        self.execute_calls: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list[object]]] = []
        self.copy_calls: list[str] = []
        self.copy_instance = RecordingCopy()
        self.fetchone_value = fetchone_value

    def execute(self, sql: str, params: object = None) -> None:
        self.execute_calls.append((sql, params))

    def executemany(self, sql: str, params: object) -> None:
        self.executemany_calls.append((sql, list(params)))  # type: ignore[arg-type]

    def copy(self, sql: str) -> RecordingCopy:
        self.copy_calls.append(sql)
        return self.copy_instance

    def fetchone(self) -> tuple[object, ...] | None:
        return self.fetchone_value


class RecordingConnection:
    def __init__(self, fetchone_value: tuple[object, ...] | None = None) -> None:
        self.cursor_instance = RecordingCursor(fetchone_value)
        self.transactions = 0

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance

    def transaction(self) -> Context:
        self.transactions += 1
        return Context()


def snapshot(vehicle_id: str = "a") -> VehicleSnapshot:
    return VehicleSnapshot(
        vehicle_id=vehicle_id,
        vehicle_type_id="scooter",
        latitude=47.61,
        longitude=-122.33,
        is_reserved=False,
        is_disabled=False,
        available_flag=True,
        grid_id="GRID_1",
        run_id=RUN_ID,
        snapshot_timestamp=TIMESTAMP,
        source_timestamp=TIMESTAMP,
        ingestion_timestamp=TIMESTAMP,
        quality_flag="SUCCESS",
    )


def pipeline_run(status: str = "SUCCESS") -> PipelineRun:
    return PipelineRun(
        run_id=RUN_ID,
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
        http_status=200,
        records_received=1,
        records_valid=1,
        records_rejected=0,
        api_latency_ms=20,
        pipeline_duration_ms=100,
        quality_status=status,  # type: ignore[arg-type]
        error_message=None,
        observed_schema_keys=["bike_id"],
    )


def system_information() -> SystemInformationRecord:
    return SystemInformationRecord(
        "lime_seattle",
        "Lime Seattle",
        "en",
        "America/Los_Angeles",
        None,
        "Lime",
        TIMESTAMP,
        TIMESTAMP,
        RUN_ID,
    )


def station_information() -> StationInformationRecord:
    return StationInformationRecord(
        "seattle", "Seattle", None, 47.61, -122.33, None, None, TIMESTAMP, TIMESTAMP, RUN_ID
    )


def station_status() -> StationStatusSnapshot:
    return StationStatusSnapshot(
        "seattle",
        10,
        20,
        True,
        True,
        True,
        TIMESTAMP,
        TIMESTAMP,
        TIMESTAMP,
        TIMESTAMP,
        RUN_ID,
        "SUCCESS",
    )


def test_connect_uses_ssl_timeouts_and_autocommit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    expected = object()
    monkeypatch.setattr(
        load_module.psycopg,
        "connect",
        lambda **kwargs: (captured.update(kwargs), expected)[1],
    )
    settings = DatabaseSettings("db", 5432, "postgres", "fleet", "secret")

    assert load_module.connect(settings) is expected
    assert captured["sslmode"] == "require"
    assert captured["autocommit"] is True
    assert "statement_timeout=120000" in str(captured["options"])


@pytest.mark.parametrize(("row", "expected"), [((25,), 25), ((None,), None), (None, None)])
def test_recent_baseline(row: tuple[object, ...] | None, expected: int | None) -> None:
    conn = RecordingConnection(row)
    assert load_module.get_recent_baseline_count(conn) == expected  # type: ignore[arg-type]
    assert "limit 96" in conn.cursor_instance.execute_calls[0][0]


def test_copy_stage_contains_all_vehicle_fields() -> None:
    conn = RecordingConnection()
    load_module._stage_vehicle_snapshots(conn, [snapshot()])  # type: ignore[arg-type]
    assert "create temporary table" in conn.cursor_instance.execute_calls[0][0]
    assert len(conn.cursor_instance.copy_instance.rows[0]) == 13


def test_sql_helpers_are_set_based_and_parameterized() -> None:
    conn = RecordingConnection()
    load_module._insert_vehicle_snapshots(conn)  # type: ignore[arg-type]
    load_module._upsert_current_state(conn)  # type: ignore[arg-type]
    load_module._delete_stale_current_state(conn, RUN_ID)  # type: ignore[arg-type]
    load_module._apply_retention(conn, 24, 35, 90, 14)  # type: ignore[arg-type]

    statements = "\n".join(sql for sql, _ in conn.cursor_instance.execute_calls)
    assert "from fleet_snapshot_stage" in statements
    assert "on conflict (vehicle_id) do update" in statements
    assert "run_id <> %s" in statements
    assert "interval '1 hour'" in statements
    assert "interval '1 day'" in statements
    assert [params for sql, params in conn.cursor_instance.execute_calls if "delete from" in sql]


def test_rejected_aggregate_threshold_and_run_writes() -> None:
    conn = RecordingConnection()
    rejected = RejectedRecord("a", {"bike_id": "a"}, "BAD", "bad record")
    load_module._insert_rejected_records(conn, RUN_ID, [])  # type: ignore[arg-type]
    load_module._insert_rejected_records(conn, RUN_ID, [rejected])  # type: ignore[arg-type]
    load_module._replace_grid_aggregates(
        conn,
        load_module.build_grid_15min_rows([snapshot()]),  # type: ignore[arg-type]
    )
    load_module._upsert_reporting_thresholds(conn, 10, 5)  # type: ignore[arg-type]
    load_module._insert_pipeline_run(conn, pipeline_run())  # type: ignore[arg-type]

    many_sql = "\n".join(sql for sql, _ in conn.cursor_instance.executemany_calls)
    execute_sql = "\n".join(sql for sql, _ in conn.cursor_instance.execute_calls)
    assert "insert into rejected_records" in many_sql
    assert "insert into fact_grid_15min" in many_sql
    assert "insert into reporting_thresholds" in many_sql
    assert "insert into pipeline_runs" in execute_sql


def test_related_feed_load_helpers_are_idempotent_and_parameterized() -> None:
    conn = RecordingConnection()
    load_module._upsert_system_information(conn, system_information())  # type: ignore[arg-type]
    load_module._replace_station_information(
        conn,
        [station_information()],
        RUN_ID,  # type: ignore[arg-type]
    )
    load_module._insert_station_status_snapshots(
        conn,
        [station_status()],  # type: ignore[arg-type]
    )
    load_module._replace_current_station_status(
        conn,
        [station_status()],
        RUN_ID,  # type: ignore[arg-type]
    )
    load_module._insert_feed_metrics(
        conn,  # type: ignore[arg-type]
        RUN_ID,
        [
            FeedRunMetric(
                "station_status", "https://example.test/status", 200, 5, TIMESTAMP, 1, 1, 0, []
            )
        ],
    )
    statements = "\n".join(sql for sql, _ in conn.cursor_instance.execute_calls)
    many_statements = "\n".join(sql for sql, _ in conn.cursor_instance.executemany_calls)
    assert "on conflict (system_id) do update" in statements
    assert "delete from station_information where run_id <> %s" in statements
    assert "fact_station_status_snapshot" in many_statements
    assert "on conflict (station_id) do update" in many_statements
    assert "on conflict (run_id, feed_name) do update" in many_statements


def test_empty_aggregates_are_a_noop() -> None:
    conn = RecordingConnection()
    load_module._replace_grid_aggregates(conn, [])  # type: ignore[arg-type]
    assert conn.cursor_instance.execute_calls == []


def test_snapshot_orchestration_preserves_failed_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    for name in (
        "_stage_vehicle_snapshots",
        "_insert_vehicle_snapshots",
        "_upsert_current_state",
        "_delete_stale_current_state",
        "_replace_grid_aggregates",
        "_insert_rejected_records",
        "_upsert_reporting_thresholds",
        "_apply_retention",
        "_insert_pipeline_run",
    ):
        monkeypatch.setattr(
            load_module,
            name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name),
        )

    kwargs: dict[str, Any] = {
        "conn": RecordingConnection(),
        "records": [snapshot()],
        "rejected_records": [],
        "pipeline_run": pipeline_run("FAILED"),
        "detail_retention_hours": 24,
        "aggregate_retention_days": 35,
        "pipeline_run_retention_days": 90,
        "rejected_retention_days": 14,
        "rebalance_high_threshold": 10,
        "rebalance_medium_threshold": 5,
    }
    load_module.load_snapshot(**kwargs)
    assert "_insert_vehicle_snapshots" in calls
    assert "_upsert_current_state" not in calls
    assert "_replace_grid_aggregates" not in calls

    calls.clear()
    kwargs["pipeline_run"] = pipeline_run("SUCCESS")
    load_module.load_snapshot(**kwargs)
    assert "_upsert_current_state" in calls
    assert "_delete_stale_current_state" in calls
    assert calls[-1] == "_insert_pipeline_run"


def test_failed_and_final_run_helpers_open_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(load_module, "_insert_pipeline_run", lambda *_: None)
    load_module.log_failed_run(conn, pipeline_run())  # type: ignore[arg-type]
    load_module.upsert_pipeline_run(conn, pipeline_run())  # type: ignore[arg-type]
    assert conn.transactions == 2
