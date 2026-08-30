import pytest

from src.config import DEFAULT_GBFS_URL, load_dotenv, load_settings


def test_load_dotenv_reads_file_without_overwriting_existing_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # local database settings
        DB_HOST=db.example.supabase.co
        DB_PORT=6543
        DB_USER="postgres.user"
        export DB_NAME=postgres
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("DB_PORT", "5432")

    load_dotenv(env_file)

    assert __import__("os").environ["DB_HOST"] == "db.example.supabase.co"
    assert __import__("os").environ["DB_PORT"] == "5432"
    assert __import__("os").environ["DB_USER"] == "postgres.user"
    assert __import__("os").environ["DB_NAME"] == "postgres"


def _set_minimal_environment(monkeypatch):
    required = {
        "DB_HOST": "db.example.test",
        "DB_NAME": "postgres",
        "DB_USER": "postgres",
        "DB_PASSWORD": "secret",
    }
    optional = [
        "GBFS_URL",
        "DB_PORT",
        "DETAIL_RETENTION_DAYS",
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
    ]
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    for name in optional:
        monkeypatch.setenv(name, "")


def test_blank_optional_ci_values_use_defaults(monkeypatch):
    _set_minimal_environment(monkeypatch)

    settings = load_settings()

    assert settings.gbfs_url == DEFAULT_GBFS_URL
    assert settings.db_port == 5432
    assert settings.volume_drop_policy == "WARNING"
    assert settings.detail_retention_days == 3
    assert settings.rejected_retention_days == 3


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DB_PORT", "70000", "DB_PORT"),
        ("DETAIL_RETENTION_DAYS", "-1", "DETAIL_RETENTION_DAYS"),
        ("REJECTED_RETENTION_DAYS", "0", "REJECTED_RETENTION_DAYS"),
        ("FEED_STALE_MINUTES", "0", "FEED_STALE_MINUTES"),
        ("VOLUME_DROP_THRESHOLD", "1.5", "VOLUME_DROP_THRESHOLD"),
        ("GRID_SIZE_DEGREES", "0", "GRID_SIZE_DEGREES"),
        ("REBALANCE_HIGH_THRESHOLD", "-1", "REBALANCE_HIGH_THRESHOLD"),
    ],
)
def test_unsafe_numeric_configuration_is_rejected(monkeypatch, name, value, message):
    _set_minimal_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        load_settings()


def test_rebalancing_high_threshold_cannot_be_below_medium(monkeypatch):
    _set_minimal_environment(monkeypatch)
    monkeypatch.setenv("REBALANCE_HIGH_THRESHOLD", "4")
    monkeypatch.setenv("REBALANCE_MEDIUM_THRESHOLD", "5")

    with pytest.raises(ValueError, match="greater than or equal"):
        load_settings()


def test_invalid_bounding_box_order_is_rejected(monkeypatch):
    _set_minimal_environment(monkeypatch)
    monkeypatch.setenv("SEATTLE_BBOX_MIN_LAT", "48")
    monkeypatch.setenv("SEATTLE_BBOX_MAX_LAT", "47")
    monkeypatch.setenv("SEATTLE_BBOX_MIN_LON", "-123")
    monkeypatch.setenv("SEATTLE_BBOX_MAX_LON", "-122")

    with pytest.raises(ValueError, match="latitude bounds"):
        load_settings()
