from pathlib import Path

import pytest

from fleet_intelligence.config import (
    DEFAULT_FREE_BIKE_STATUS_URL,
    DEFAULT_GBFS_URL,
    DEFAULT_STATION_INFORMATION_URL,
    DEFAULT_STATION_STATUS_URL,
    DEFAULT_SYSTEM_INFORMATION_URL,
    load_database_settings,
    load_environment,
    load_pipeline_settings,
)

PIPELINE_ENV = (
    "GBFS_URL",
    "SYSTEM_INFORMATION_URL",
    "STATION_INFORMATION_URL",
    "STATION_STATUS_URL",
    "FREE_BIKE_STATUS_URL",
    "DETAIL_RETENTION_HOURS",
    "AGGREGATE_RETENTION_DAYS",
    "PIPELINE_RUN_RETENTION_DAYS",
    "REJECTED_RETENTION_DAYS",
    "FEED_STALE_MINUTES",
    "VOLUME_DROP_THRESHOLD",
    "VOLUME_DROP_POLICY",
    "SEATTLE_BBOX_MIN_LAT",
    "SEATTLE_BBOX_MAX_LAT",
    "SEATTLE_BBOX_MIN_LON",
    "SEATTLE_BBOX_MAX_LON",
    "GRID_SIZE_DEGREES",
    "REBALANCE_HIGH_THRESHOLD",
    "REBALANCE_MEDIUM_THRESHOLD",
)
DATABASE_ENV = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_SSLMODE",
    "DB_CONNECT_TIMEOUT_SECONDS",
    "DB_STATEMENT_TIMEOUT_SECONDS",
)


def clear_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PIPELINE_ENV + DATABASE_ENV:
        monkeypatch.delenv(name, raising=False)


def set_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_HOST", "db.example.test")
    monkeypatch.setenv("DB_NAME", "postgres")
    monkeypatch.setenv("DB_USER", "fleet")
    monkeypatch.setenv("DB_PASSWORD", "secret")


def test_load_environment_reads_dotenv_without_overwriting_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DB_HOST=db.example.test\n"
        "DB_PORT=6543\n"
        'DB_USER="fleet.user"\n'
        "export DB_NAME=postgres\n"
        "DB_PASSWORD=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DB_PORT", "5432")

    load_environment(env_file)

    assert load_database_settings(tmp_path / "missing").port == 5432


def test_pipeline_defaults_are_production_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_environment(monkeypatch)
    settings = load_pipeline_settings(tmp_path / "missing")
    assert settings.gbfs_url == DEFAULT_GBFS_URL
    assert settings.system_information_url == DEFAULT_SYSTEM_INFORMATION_URL
    assert settings.station_information_url == DEFAULT_STATION_INFORMATION_URL
    assert settings.station_status_url == DEFAULT_STATION_STATUS_URL
    assert settings.free_bike_status_url == DEFAULT_FREE_BIKE_STATUS_URL
    assert list(settings.feed_urls) == [
        "system_information",
        "station_information",
        "station_status",
        "free_bike_status",
    ]
    assert settings.detail_retention_hours == 24
    assert settings.aggregate_retention_days == 35
    assert settings.pipeline_run_retention_days == 90
    assert settings.rejected_retention_days == 14
    assert settings.feed_stale_minutes == 10
    assert settings.volume_drop_policy == "WARNING"


def test_database_defaults_and_password_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_environment(monkeypatch)
    set_database_environment(monkeypatch)
    settings = load_database_settings(tmp_path / "missing")
    assert settings.port == 5432
    assert settings.sslmode == "require"
    assert settings.password not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DETAIL_RETENTION_HOURS", "0", "DETAIL_RETENTION_HOURS"),
        ("AGGREGATE_RETENTION_DAYS", "0", "AGGREGATE_RETENTION_DAYS"),
        ("PIPELINE_RUN_RETENTION_DAYS", "-1", "PIPELINE_RUN_RETENTION_DAYS"),
        ("REJECTED_RETENTION_DAYS", "0", "REJECTED_RETENTION_DAYS"),
        ("FEED_STALE_MINUTES", "0", "FEED_STALE_MINUTES"),
        ("VOLUME_DROP_THRESHOLD", "1.5", "VOLUME_DROP_THRESHOLD"),
        ("GRID_SIZE_DEGREES", "0", "GRID_SIZE_DEGREES"),
        ("REBALANCE_HIGH_THRESHOLD", "-1", "thresholds"),
        ("DETAIL_RETENTION_HOURS", "not-an-int", "must be an integer"),
        ("VOLUME_DROP_THRESHOLD", "not-a-number", "must be a number"),
    ],
)
def test_invalid_pipeline_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    clear_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        load_pipeline_settings(tmp_path / "missing")


def test_invalid_policy_url_threshold_order_and_bbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_environment(monkeypatch)
    monkeypatch.setenv("VOLUME_DROP_POLICY", "ignore")
    with pytest.raises(ValueError, match="WARNING or FAILED"):
        load_pipeline_settings(tmp_path / "missing")

    monkeypatch.setenv("VOLUME_DROP_POLICY", "WARNING")
    monkeypatch.setenv("GBFS_URL", "http://example.test/feed")
    with pytest.raises(ValueError, match="HTTPS"):
        load_pipeline_settings(tmp_path / "missing")

    monkeypatch.setenv("GBFS_URL", DEFAULT_GBFS_URL)
    monkeypatch.setenv("REBALANCE_HIGH_THRESHOLD", "4")
    monkeypatch.setenv("REBALANCE_MEDIUM_THRESHOLD", "5")
    with pytest.raises(ValueError, match="greater than or equal"):
        load_pipeline_settings(tmp_path / "missing")

    monkeypatch.setenv("REBALANCE_HIGH_THRESHOLD", "10")
    monkeypatch.setenv("SEATTLE_BBOX_MIN_LAT", "48")
    with pytest.raises(ValueError, match="Incomplete"):
        load_pipeline_settings(tmp_path / "missing")


def test_bounding_box_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_environment(monkeypatch)
    values = {
        "SEATTLE_BBOX_MIN_LAT": "47",
        "SEATTLE_BBOX_MAX_LAT": "48",
        "SEATTLE_BBOX_MIN_LON": "-123",
        "SEATTLE_BBOX_MAX_LON": "-122",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = load_pipeline_settings(tmp_path / "missing")
    assert settings.bbox is not None
    assert settings.bbox.contains(47.5, -122.5)

    monkeypatch.setenv("SEATTLE_BBOX_MAX_LAT", "bad")
    with pytest.raises(ValueError, match="must be numbers"):
        load_pipeline_settings(tmp_path / "missing")

    monkeypatch.setenv("SEATTLE_BBOX_MAX_LAT", "46")
    with pytest.raises(ValueError, match="latitude bounds"):
        load_pipeline_settings(tmp_path / "missing")

    monkeypatch.setenv("SEATTLE_BBOX_MAX_LAT", "48")
    monkeypatch.setenv("SEATTLE_BBOX_MIN_LON", "10")
    monkeypatch.setenv("SEATTLE_BBOX_MAX_LON", "-10")
    with pytest.raises(ValueError, match="longitude bounds"):
        load_pipeline_settings(tmp_path / "missing")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DB_PORT", "70000", "DB_PORT"),
        ("DB_SSLMODE", "disable", "DB_SSLMODE"),
        ("DB_CONNECT_TIMEOUT_SECONDS", "0", "DB_CONNECT_TIMEOUT_SECONDS"),
        ("DB_STATEMENT_TIMEOUT_SECONDS", "0", "DB_STATEMENT_TIMEOUT_SECONDS"),
    ],
)
def test_invalid_database_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    clear_environment(monkeypatch)
    set_database_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        load_database_settings(tmp_path / "missing")


def test_required_database_value_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_environment(monkeypatch)
    with pytest.raises(ValueError, match="DB_HOST"):
        load_database_settings(tmp_path / "missing")
