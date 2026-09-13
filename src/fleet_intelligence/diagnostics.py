"""Explicit read-only checks for live external dependencies."""

from __future__ import annotations

import argparse
import logging

from .config import load_database_settings, load_pipeline_settings
from .extract import fetch_gbfs_feeds
from .load import connect, get_recent_baseline_count
from .validate import (
    validate_free_bike_status,
    validate_station_information,
    validate_station_status,
    validate_system_information,
)

LOGGER = logging.getLogger(__name__)
REQUIRED_RELATIONS = (
    "current_vehicle_state",
    "fact_vehicle_snapshot",
    "fact_grid_15min",
    "rejected_records",
    "pipeline_runs",
    "reporting_thresholds",
    "system_information",
    "station_information",
    "current_station_status",
    "fact_station_status_snapshot",
    "feed_run_metrics",
    "vw_current_station_supply",
    "vw_feed_health",
)


def check_live_feed() -> None:
    settings = load_pipeline_settings()
    results = fetch_gbfs_feeds(settings.feed_urls, retries=1)
    system = validate_system_information(results["system_information"].payload)
    stations = validate_station_information(results["station_information"].payload, settings.bbox)
    statuses = validate_station_status(results["station_status"].payload)
    vehicles = validate_free_bike_status(results["free_bike_status"].payload, settings.bbox)
    if not vehicles.accepted_records:
        raise RuntimeError("Live feed produced zero valid vehicle records")
    if not stations.accepted_records or not statuses.accepted_records:
        raise RuntimeError("Live feed produced zero valid station records")
    LOGGER.info(
        "diagnostic=live_feeds status=pass system=%s stations=%d statuses=%d "
        "vehicles=%d rejected=%d",
        system.record.system_id,
        len(stations.accepted_records),
        len(statuses.accepted_records),
        len(vehicles.accepted_records),
        len(stations.rejected_records)
        + len(statuses.rejected_records)
        + len(vehicles.rejected_records),
    )


def check_database() -> None:
    conn = connect(load_database_settings())
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                select relation_name
                from unnest(%s::text[]) relation_name
                where to_regclass('public.' || relation_name) is null
                """,
                (list(REQUIRED_RELATIONS),),
            )
            missing = [str(row[0]) for row in cursor.fetchall()]
        if missing:
            raise RuntimeError(f"Database schema is missing: {', '.join(missing)}")
        LOGGER.info(
            "diagnostic=database status=pass recent_baseline=%s",
            get_recent_baseline_count(conn),
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only fleet dependency diagnostics.")
    parser.add_argument("--live", action="store_true", help="Check all configured GBFS feeds.")
    parser.add_argument(
        "--database",
        action="store_true",
        help="Check PostgreSQL connectivity and required relations.",
    )
    args = parser.parse_args()
    if not args.live and not args.database:
        parser.error("select at least one of --live or --database")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        force=True,
    )
    try:
        if args.live:
            check_live_feed()
        if args.database:
            check_database()
    except Exception:
        LOGGER.exception("diagnostic status=failed")
        return 1
    return 0
