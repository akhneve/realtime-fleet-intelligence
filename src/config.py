"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_GBFS_URL = "https://data.lime.bike/api/partners/v1/gbfs/seattle/free_bike_status.json"  # What: default Lime Seattle GBFS feed. Why: local/dev runs should work without requiring a secret for the public URL.
DEFAULT_SOURCE_FEED = "lime_seattle_free_bike_status"  # What: stable internal source label. Why: downstream tables need a human-readable lineage value.
PROJECT_ROOT = Path(__file__).resolve().parents[1]  # What: absolute repo root path. Why: config can find .env no matter where Python is launched from.
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"  # What: standard local config file path. Why: local runs should pick up your credentials automatically.


@dataclass(frozen=True)
class BoundingBox:
    # What: optional coordinate fence for accepted records. Why: lets the pipeline quarantine out-of-market vehicles without hard-coding Seattle limits.
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    def contains(self, latitude: float, longitude: float) -> bool:
        # What: checks whether a point is inside the configured bounds. Why: validation needs a single reusable rule for out-of-bounds records.
        return self.min_lat <= latitude <= self.max_lat and self.min_lon <= longitude <= self.max_lon


@dataclass(frozen=True)
class Settings:
    # What: typed container for all runtime settings. Why: passing one object around is safer than repeatedly reading environment variables.
    gbfs_url: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    detail_retention_days: int
    rejected_retention_days: int
    feed_stale_minutes: int | None
    volume_drop_threshold: float
    volume_drop_policy: str
    bbox: BoundingBox | None
    grid_size_degrees: float
    rebalance_high_threshold: int
    rebalance_medium_threshold: int
    source_feed: str = DEFAULT_SOURCE_FEED


def _required(name: str) -> str:
    # What: reads an environment variable that must exist. Why: database credentials must fail fast instead of causing confusing connection errors later.
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def load_dotenv(path: Path = DEFAULT_ENV_FILE) -> None:
    # What: loads KEY=value pairs from a local .env file into os.environ. Why: local testing should not require manually exporting every variable.
    if not path.exists():
        # What: silently skips when .env is absent. Why: GitHub Actions will use real environment variables/secrets instead of a local file.
        return

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # What: trims whitespace and ignores blank/comment lines. Why: .env files are often organized with spacing and comments.
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            # What: accepts shell-style "export KEY=value" lines. Why: this makes the parser tolerant of common .env formats.
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=value")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid .env line {line_number}: key cannot be empty")

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            # What: removes matching surrounding quotes. Why: DB passwords and URLs are commonly quoted in .env files.
            value = value[1:-1]

        os.environ.setdefault(key, value)  # What: only fills missing variables. Why: CI secrets and explicit shell vars should override .env.


def _int_env(name: str, default: int | None = None) -> int | None:
    # What: reads an optional integer environment value. Why: scheduler/retention thresholds should be configurable without code edits.
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _float_env(name: str, default: float | None = None) -> float | None:
    # What: reads an optional decimal environment value. Why: anomaly thresholds and grid sizes need decimal precision.
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _text_env(name: str, default: str) -> str:
    # What: reads a text setting while treating blank environment variables as absent. Why: CI injects missing optional secrets as empty strings.
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _bbox_from_env() -> BoundingBox | None:
    # What: builds a BoundingBox only when all four coordinate settings are present. Why: partial geographic config is risky and should fail loudly.
    names = [
        "SEATTLE_BBOX_MIN_LAT",
        "SEATTLE_BBOX_MAX_LAT",
        "SEATTLE_BBOX_MIN_LON",
        "SEATTLE_BBOX_MAX_LON",
    ]
    values = [os.getenv(name) for name in names]
    if all(value is None or value.strip() == "" for value in values):
        return None
    if any(value is None or value.strip() == "" for value in values):
        missing = [name for name, value in zip(names, values) if value is None or value.strip() == ""]
        raise ValueError(f"Incomplete Seattle bounding box configuration: {', '.join(missing)}")
    bbox = BoundingBox(*(float(value) for value in values if value is not None))
    if not (-90 <= bbox.min_lat < bbox.max_lat <= 90):
        raise ValueError("Seattle latitude bounds must satisfy -90 <= min < max <= 90")
    if not (-180 <= bbox.min_lon < bbox.max_lon <= 180):
        raise ValueError("Seattle longitude bounds must satisfy -180 <= min < max <= 180")
    return bbox


def load_settings() -> Settings:
    # What: creates the final Settings object used by the ingestion run. Why: centralizing config keeps secrets and thresholds out of business logic.
    load_dotenv()  # What: imports local .env values before validation. Why: DB_HOST and other required settings can live in .env as requested.
    volume_drop_policy = _text_env("VOLUME_DROP_POLICY", "WARNING").upper()
    if volume_drop_policy not in {"WARNING", "FAILED"}:
        raise ValueError("VOLUME_DROP_POLICY must be WARNING or FAILED")

    db_port = _int_env("DB_PORT", 5432)
    detail_retention_days = _int_env("DETAIL_RETENTION_DAYS", 3)
    rejected_retention_days = _int_env("REJECTED_RETENTION_DAYS", 3)
    feed_stale_minutes = _int_env("FEED_STALE_MINUTES")
    volume_drop_threshold = _float_env("VOLUME_DROP_THRESHOLD", 0.50)
    grid_size_degrees = _float_env("GRID_SIZE_DEGREES", 0.01)
    rebalance_high_threshold = _int_env("REBALANCE_HIGH_THRESHOLD", 10)
    rebalance_medium_threshold = _int_env("REBALANCE_MEDIUM_THRESHOLD", 5)

    if db_port is None or not 1 <= db_port <= 65535:
        raise ValueError("DB_PORT must be between 1 and 65535")
    if detail_retention_days is None or detail_retention_days < 1:
        raise ValueError("DETAIL_RETENTION_DAYS must be at least 1")
    if rejected_retention_days is None or rejected_retention_days < 1:
        raise ValueError("REJECTED_RETENTION_DAYS must be at least 1")
    if feed_stale_minutes is not None and feed_stale_minutes < 1:
        raise ValueError("FEED_STALE_MINUTES must be at least 1 when configured")
    if volume_drop_threshold is None or not 0 <= volume_drop_threshold <= 1:
        raise ValueError("VOLUME_DROP_THRESHOLD must be between 0 and 1")
    if grid_size_degrees is None or not 0 < grid_size_degrees <= 180:
        raise ValueError("GRID_SIZE_DEGREES must be greater than 0 and at most 180")
    if rebalance_high_threshold is None or rebalance_high_threshold < 0:
        raise ValueError("REBALANCE_HIGH_THRESHOLD must be non-negative")
    if rebalance_medium_threshold is None or rebalance_medium_threshold < 0:
        raise ValueError("REBALANCE_MEDIUM_THRESHOLD must be non-negative")
    if rebalance_high_threshold < rebalance_medium_threshold:
        raise ValueError("REBALANCE_HIGH_THRESHOLD must be greater than or equal to REBALANCE_MEDIUM_THRESHOLD")

    return Settings(
        gbfs_url=_text_env("GBFS_URL", DEFAULT_GBFS_URL),
        db_host=_required("DB_HOST"),
        db_port=db_port,
        db_name=_required("DB_NAME"),
        db_user=_required("DB_USER"),
        db_password=_required("DB_PASSWORD"),
        detail_retention_days=detail_retention_days,
        rejected_retention_days=rejected_retention_days,
        feed_stale_minutes=feed_stale_minutes,
        volume_drop_threshold=volume_drop_threshold,
        volume_drop_policy=volume_drop_policy,
        bbox=_bbox_from_env(),
        grid_size_degrees=grid_size_degrees,
        rebalance_high_threshold=rebalance_high_threshold,
        rebalance_medium_threshold=rebalance_medium_threshold,
    )


def _smoke_test() -> None:
    """Run with: python -m src.config"""
    # What: supplies harmless fake DB values for local config testing. Why: you can verify parsing/defaults without needing real credentials.
    os.environ.setdefault("DB_HOST", "localhost")
    os.environ.setdefault("DB_NAME", "fleet")
    os.environ.setdefault("DB_USER", "fleet_user")
    os.environ.setdefault("DB_PASSWORD", "not-a-real-password")

    settings = load_settings()
    print("Config smoke test passed")
    print(f"GBFS URL: {settings.gbfs_url}")
    print(f"DB target: {settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}")
    print(f"Detail retention days: {settings.detail_retention_days}")
    print(f"Bounding box configured: {settings.bbox is not None}")


if __name__ == "__main__":
    _smoke_test()
