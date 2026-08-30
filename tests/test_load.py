from types import SimpleNamespace

import src.load as load_module


def test_connect_uses_autocommit_so_explicit_write_transactions_can_commit(monkeypatch):
    captured: dict[str, object] = {}
    expected_connection = object()

    class FakePsycopg:
        @staticmethod
        def connect(**kwargs):
            captured.update(kwargs)
            return expected_connection

    monkeypatch.setattr(load_module, "psycopg", FakePsycopg)
    settings = SimpleNamespace(
        db_host="db.example.test",
        db_port=5432,
        db_name="postgres",
        db_user="postgres",
        db_password="secret",
    )

    connection = load_module.connect(settings)

    assert connection is expected_connection
    assert captured["autocommit"] is True
    assert captured["connect_timeout"] == 10


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _TransactionConnection:
    def transaction(self):
        return _Transaction()


def _patch_snapshot_helpers(monkeypatch, calls):
    monkeypatch.setattr(load_module, "build_grid_15min_rows", lambda records: ["aggregate"])
    for name in [
        "_insert_vehicle_snapshots",
        "_upsert_current_state",
        "_delete_stale_current_state",
        "_replace_grid_aggregates",
        "_insert_rejected_records",
        "_upsert_reporting_thresholds",
        "_apply_retention",
        "_insert_pipeline_run",
    ]:
        monkeypatch.setattr(load_module, name, lambda *args, _name=name: calls.append(_name))


def _load_snapshot(quality_status):
    return {
        "conn": _TransactionConnection(),
        "records": [{"vehicle_id": "a"}],
        "rejected_records": [],
        "pipeline_run": {"run_id": "00000000-0000-0000-0000-000000000001", "quality_status": quality_status},
        "detail_retention_days": 3,
        "rejected_retention_days": 3,
        "rebalance_high_threshold": 10,
        "rebalance_medium_threshold": 5,
    }


def test_successful_snapshot_reconciles_current_state_and_replaces_grid_bucket(monkeypatch):
    calls = []
    _patch_snapshot_helpers(monkeypatch, calls)

    load_module.load_snapshot(**_load_snapshot("SUCCESS"))

    assert "_upsert_current_state" in calls
    assert "_delete_stale_current_state" in calls
    assert "_replace_grid_aggregates" in calls
    assert calls[-1] == "_insert_pipeline_run"


def test_failed_snapshot_does_not_change_current_state_or_grid_history(monkeypatch):
    calls = []
    _patch_snapshot_helpers(monkeypatch, calls)

    load_module.load_snapshot(**_load_snapshot("FAILED"))

    assert "_upsert_current_state" not in calls
    assert "_delete_stale_current_state" not in calls
    assert "_replace_grid_aggregates" not in calls
    assert "_insert_vehicle_snapshots" in calls
    assert "_insert_pipeline_run" in calls


class _RecordingCursor:
    def __init__(self):
        self.executemany_calls = []
        self.execute_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def executemany(self, sql, params):
        self.executemany_calls.append((sql, list(params)))

    def execute(self, sql, params):
        self.execute_calls.append((sql, params))


class _CursorConnection:
    def __init__(self):
        self.cursor_instance = _RecordingCursor()

    def cursor(self):
        return self.cursor_instance


def test_grid_replacement_deletes_bucket_before_inserting_rows():
    from datetime import datetime, timezone

    conn = _CursorConnection()
    bucket = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    load_module._replace_grid_aggregates(
        conn,
        [
            {
                "bucket_timestamp": bucket,
                "grid_id": "GRID_1",
                "total_vehicles": 1,
                "available_vehicles": 1,
                "unavailable_vehicles": 0,
                "run_id": "00000000-0000-0000-0000-000000000001",
            }
        ],
    )

    delete_sql, delete_params = conn.cursor_instance.executemany_calls[0]
    insert_sql, _ = conn.cursor_instance.executemany_calls[1]
    assert "delete from fact_grid_15min" in delete_sql
    assert delete_params == [(bucket,)]
    assert "insert into fact_grid_15min" in insert_sql


def test_stale_current_rows_are_deleted_by_run_id():
    conn = _CursorConnection()

    load_module._delete_stale_current_state(conn, "00000000-0000-0000-0000-000000000001")

    sql, params = conn.cursor_instance.execute_calls[0]
    assert "delete from current_vehicle_state" in sql
    assert params == ("00000000-0000-0000-0000-000000000001",)


def test_retention_covers_all_historical_tables():
    conn = _CursorConnection()

    load_module._apply_retention(conn, 3, 3)

    statements = "\n".join(sql for sql, _ in conn.cursor_instance.execute_calls)
    assert "delete from fact_vehicle_snapshot" in statements
    assert "delete from fact_grid_15min" in statements
    assert "delete from rejected_records" in statements
    assert "delete from pipeline_runs" in statements
    assert all(params == (3,) for _, params in conn.cursor_instance.execute_calls)
