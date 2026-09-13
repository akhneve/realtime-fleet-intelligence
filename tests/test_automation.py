from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ingestion_is_small_hardened_and_every_fifteen_minutes() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ingest.yml").read_text(encoding="utf-8")
    assert 'cron: "*/15 * * * *"' in workflow
    assert "timeout-minutes: 12" in workflow
    assert "contents: read" in workflow
    assert "python -m pytest" not in workflow
    assert "DETAIL_RETENTION_HOURS" in workflow
    assert "AGGREGATE_RETENTION_DAYS" in workflow
    assert "SYSTEM_INFORMATION_URL" in workflow
    assert "STATION_INFORMATION_URL" in workflow
    assert "STATION_STATUS_URL" in workflow
    assert "FREE_BIKE_STATUS_URL" in workflow


def test_ci_has_two_python_versions_and_postgres_integration() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'python-version: ["3.12", "3.13"]' in workflow
    assert "image: postgis/postgis:17-3.5" in workflow
    assert "--cov-fail-under=90" in workflow
    assert "python -m mypy" in workflow
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow


def test_geography_refresh_runs_monthly_and_can_be_dispatched() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "geography.yml").read_text(
        encoding="utf-8"
    )
    assert 'cron: "17 9 1 * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "python scripts/ingest_geography.py" in workflow
    assert "GEOGRAPHY_GRID_SIZE_DEGREES" in workflow
    assert "cancel-in-progress: false" in workflow
