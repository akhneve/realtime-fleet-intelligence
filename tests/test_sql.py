from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_reporting_views_use_seattle_local_time():
    sql = (PROJECT_ROOT / "sql" / "003_views.sql").read_text(encoding="utf-8")

    assert "bucket_timestamp at time zone 'America/Los_Angeles'" in sql
    assert "now() at time zone 'America/Los_Angeles'" in sql


def test_rebalancing_candidates_include_historical_and_current_grids():
    sql = (PROJECT_ROOT / "sql" / "003_views.sql").read_text(encoding="utf-8")

    assert "candidate_grids as" in sql
    assert "select grid_id from current_supply" in sql
    assert "select grid_id from context_baseline" in sql


def test_security_migration_revokes_api_roles_and_enables_rls():
    sql = (PROJECT_ROOT / "sql" / "004_security.sql").read_text(encoding="utf-8")

    assert "revoke all privileges on all tables in schema public from anon, authenticated" in sql
    for table in [
        "current_vehicle_state",
        "fact_vehicle_snapshot",
        "fact_grid_15min",
        "rejected_records",
        "pipeline_runs",
        "reporting_thresholds",
    ]:
        assert f"alter table {table} enable row level security" in sql


def test_workflow_runs_twice_daily_in_pacific_time():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ingest.yml").read_text(encoding="utf-8")

    assert 'cron: "0 9,21 * * *"' in workflow
    assert 'timezone: "America/Los_Angeles"' in workflow
    assert 'DETAIL_RETENTION_DAYS: "3"' in workflow
    assert 'REJECTED_RETENTION_DAYS: "3"' in workflow
