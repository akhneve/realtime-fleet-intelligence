"""Validated environment-backed configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from dotenv import load_dotenv

from .models import FeedName, VolumeDropPolicy

GBFS_BASE_URL = "https://data.lime.bike/api/partners/v1/gbfs/seattle"
DEFAULT_SYSTEM_INFORMATION_URL = f"{GBFS_BASE_URL}/system_information.json"
DEFAULT_STATION_INFORMATION_URL = f"{GBFS_BASE_URL}/station_information.json"
DEFAULT_STATION_STATUS_URL = f"{GBFS_BASE_URL}/station_status.json"
DEFAULT_FREE_BIKE_STATUS_URL = f"{GBFS_BASE_URL}/free_bike_status.json"
# Kept as a public alias for deployments that still use the original setting name.
DEFAULT_GBFS_URL = DEFAULT_FREE_BIKE_STATUS_URL
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Optional deployment boundary for accepted vehicle coordinates."""

    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    def contains(self, latitude: float, longitude: float) -> bool:
        return (
            self.min_lat <= latitude <= self.max_lat and self.min_lon <= longitude <= self.max_lon
        )


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    system_information_url: str
    station_information_url: str
    station_status_url: str
    free_bike_status_url: str
    detail_retention_hours: int
    aggregate_retention_days: int
    pipeline_run_retention_days: int
    rejected_retention_days: int
    feed_stale_minutes: int
    volume_drop_threshold: float
    volume_drop_policy: VolumeDropPolicy
    bbox: BoundingBox | None
    grid_size_degrees: float
    rebalance_high_threshold: int
    rebalance_medium_threshold: int

    @property
    def gbfs_url(self) -> str:
        """Backward-compatible name for the original free-bike-only URL."""

        return self.free_bike_status_url

    @property
    def feed_urls(self) -> dict[FeedName, str]:
        return {
            "system_information": self.system_information_url,
            "station_information": self.station_information_url,
            "station_status": self.station_status_url,
            "free_bike_status": self.free_bike_status_url,
        }


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    sslmode: str = "require"
    connect_timeout_seconds: int = 10
    statement_timeout_seconds: int = 120


def load_environment(path: Path = DEFAULT_ENV_FILE) -> None:
    """Load local development values without overriding explicit environment variables."""

    if path.is_file():
        load_dotenv(dotenv_path=path, override=False, encoding="utf-8")


def load_pipeline_settings(env_file: Path = DEFAULT_ENV_FILE) -> PipelineSettings:
    load_environment(env_file)

    policy_text = _text_env("VOLUME_DROP_POLICY", "WARNING").upper()
    if policy_text not in {"WARNING", "FAILED"}:
        raise ValueError("VOLUME_DROP_POLICY must be WARNING or FAILED")
    policy = cast(VolumeDropPolicy, policy_text)

    legacy_free_bike_url = _text_env("GBFS_URL", DEFAULT_FREE_BIKE_STATUS_URL)
    settings = PipelineSettings(
        system_information_url=_https_url_env(
            "SYSTEM_INFORMATION_URL", DEFAULT_SYSTEM_INFORMATION_URL
        ),
        station_information_url=_https_url_env(
            "STATION_INFORMATION_URL", DEFAULT_STATION_INFORMATION_URL
        ),
        station_status_url=_https_url_env("STATION_STATUS_URL", DEFAULT_STATION_STATUS_URL),
        free_bike_status_url=_https_url_env("FREE_BIKE_STATUS_URL", legacy_free_bike_url),
        detail_retention_hours=_int_env("DETAIL_RETENTION_HOURS", 24),
        aggregate_retention_days=_int_env("AGGREGATE_RETENTION_DAYS", 35),
        pipeline_run_retention_days=_int_env("PIPELINE_RUN_RETENTION_DAYS", 90),
        rejected_retention_days=_int_env("REJECTED_RETENTION_DAYS", 14),
        feed_stale_minutes=_int_env("FEED_STALE_MINUTES", 10),
        volume_drop_threshold=_float_env("VOLUME_DROP_THRESHOLD", 0.50),
        volume_drop_policy=policy,
        bbox=_bbox_from_env(),
        grid_size_degrees=_float_env("GRID_SIZE_DEGREES", 0.01),
        rebalance_high_threshold=_int_env("REBALANCE_HIGH_THRESHOLD", 10),
        rebalance_medium_threshold=_int_env("REBALANCE_MEDIUM_THRESHOLD", 5),
    )
    _validate_pipeline_settings(settings)
    return settings


def load_database_settings(env_file: Path = DEFAULT_ENV_FILE) -> DatabaseSettings:
    load_environment(env_file)
    sslmode = _text_env("DB_SSLMODE", "require").lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("DB_SSLMODE must be require, verify-ca, or verify-full")

    settings = DatabaseSettings(
        host=_required("DB_HOST"),
        port=_int_env("DB_PORT", 5432),
        name=_required("DB_NAME"),
        user=_required("DB_USER"),
        password=_required("DB_PASSWORD"),
        sslmode=sslmode,
        connect_timeout_seconds=_int_env("DB_CONNECT_TIMEOUT_SECONDS", 10),
        statement_timeout_seconds=_int_env("DB_STATEMENT_TIMEOUT_SECONDS", 120),
    )
    if not 1 <= settings.port <= 65535:
        raise ValueError("DB_PORT must be between 1 and 65535")
    if settings.connect_timeout_seconds < 1:
        raise ValueError("DB_CONNECT_TIMEOUT_SECONDS must be at least 1")
    if settings.statement_timeout_seconds < 1:
        raise ValueError("DB_STATEMENT_TIMEOUT_SECONDS must be at least 1")
    return settings


def _validate_pipeline_settings(settings: PipelineSettings) -> None:
    positive_values = {
        "DETAIL_RETENTION_HOURS": settings.detail_retention_hours,
        "AGGREGATE_RETENTION_DAYS": settings.aggregate_retention_days,
        "PIPELINE_RUN_RETENTION_DAYS": settings.pipeline_run_retention_days,
        "REJECTED_RETENTION_DAYS": settings.rejected_retention_days,
        "FEED_STALE_MINUTES": settings.feed_stale_minutes,
    }
    for name, value in positive_values.items():
        if value < 1:
            raise ValueError(f"{name} must be at least 1")
    if not 0 <= settings.volume_drop_threshold <= 1:
        raise ValueError("VOLUME_DROP_THRESHOLD must be between 0 and 1")
    if not 0 < settings.grid_size_degrees <= 180:
        raise ValueError("GRID_SIZE_DEGREES must be greater than 0 and at most 180")
    if settings.rebalance_high_threshold < 0 or settings.rebalance_medium_threshold < 0:
        raise ValueError("Rebalancing thresholds must be non-negative")
    if settings.rebalance_high_threshold < settings.rebalance_medium_threshold:
        raise ValueError(
            "REBALANCE_HIGH_THRESHOLD must be greater than or equal to REBALANCE_MEDIUM_THRESHOLD"
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _text_env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; received {value!r}") from exc


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; received {value!r}") from exc


def _https_url_env(name: str, default: str) -> str:
    value = _text_env(name, default)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTPS URL")
    return value


def _bbox_from_env() -> BoundingBox | None:
    names = (
        "SEATTLE_BBOX_MIN_LAT",
        "SEATTLE_BBOX_MAX_LAT",
        "SEATTLE_BBOX_MIN_LON",
        "SEATTLE_BBOX_MAX_LON",
    )
    values = [os.getenv(name) for name in names]
    if all(value is None or not value.strip() for value in values):
        return None
    missing = [
        name
        for name, value in zip(names, values, strict=True)
        if value is None or not value.strip()
    ]
    if missing:
        raise ValueError(f"Incomplete Seattle bounding box configuration: {', '.join(missing)}")
    try:
        numbers = [float(cast(str, value)) for value in values]
    except ValueError as exc:
        raise ValueError("Seattle bounding box values must be numbers") from exc
    bbox = BoundingBox(*numbers)
    if not (-90 <= bbox.min_lat < bbox.max_lat <= 90):
        raise ValueError("Seattle latitude bounds must satisfy -90 <= min < max <= 90")
    if not (-180 <= bbox.min_lon < bbox.max_lon <= 180):
        raise ValueError("Seattle longitude bounds must satisfy -180 <= min < max <= 180")
    return bbox
