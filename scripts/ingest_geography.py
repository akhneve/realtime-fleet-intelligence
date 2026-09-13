"""Download, validate, normalize, and load Seattle geography dimensions."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import requests
from psycopg import Connection
from psycopg.types.json import Jsonb
from requests.adapters import HTTPAdapter
from shapely import Geometry, make_valid  # type: ignore[import-untyped]
from shapely.geometry import (  # type: ignore[import-untyped]
    GeometryCollection,
    MultiPolygon,
    Polygon,
    mapping,
    shape,
)
from shapely.geometry.polygon import orient  # type: ignore[import-untyped]
from shapely.validation import explain_validity  # type: ignore[import-untyped]
from urllib3.util.retry import Retry

from fleet_intelligence.config import load_database_settings, load_environment

LOGGER = logging.getLogger("geography_ingestion")
USER_AGENT = (
    "realtime-fleet-intelligence-geography/1.0 "
    "(+https://github.com/akhneve/realtime-fleet-intelligence)"
)
DEFAULT_TIMEOUT = (5.0, 60.0)


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    key: str
    source_name: str
    source_url: str
    destination_table: str


@dataclass(frozen=True, slots=True)
class ProcessedSource:
    definition: SourceDefinition
    records: list[dict[str, Any]]
    features_received: int
    features_excluded: int
    geometries_repaired: int
    property_names: tuple[str, ...]
    geometry_types: tuple[str, ...]


SOURCES = (
    SourceDefinition(
        key="neighborhoods",
        source_name="Seattle neighborhoods",
        source_url=(
            "https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/"
            "master/data/neighborhoods.geojson"
        ),
        destination_table="dim_neighborhood",
    ),
    SourceDefinition(
        key="zip_codes",
        source_name="Seattle ZIP codes",
        source_url=(
            "https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/"
            "master/data/zip-codes.geojson"
        ),
        destination_table="dim_zip_area",
    ),
    SourceDefinition(
        key="council_districts",
        source_name="Seattle city council districts",
        source_url=(
            "https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/"
            "master/data/city-council-districts.geojson"
        ),
        destination_table="dim_council_district",
    ),
)


def build_http_session() -> requests.Session:
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        backoff_factor=0.75,
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update(
        {"Accept": "application/geo+json, application/json", "User-Agent": USER_AGENT}
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def download_geojson(
    session: requests.Session,
    source: SourceDefinition,
) -> Mapping[str, Any]:
    LOGGER.info("Downloading %s from %s", source.source_name, source.source_url)
    response = session.get(source.source_url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    if not response.content:
        raise ValueError(f"{source.source_name} returned an empty response")
    try:
        document = response.json()
    except requests.JSONDecodeError as exc:
        raise ValueError(f"{source.source_name} did not return valid JSON") from exc
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ValueError(f"{source.source_name} is not a GeoJSON FeatureCollection")
    features = document.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError(f"{source.source_name} contains no features")
    LOGGER.info(
        "Downloaded %s: HTTP %s, %s bytes, %s features",
        source.source_name,
        response.status_code,
        len(response.content),
        len(features),
    )
    return cast(Mapping[str, Any], document)


def process_source(source: SourceDefinition, document: Mapping[str, Any]) -> ProcessedSource:
    raw_features = document.get("features")
    if not isinstance(raw_features, list):
        raise ValueError(f"{source.source_name} features must be a list")

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    property_names: set[str] = set()
    geometry_types: set[str] = set()
    excluded = 0
    repaired = 0

    for index, raw_feature in enumerate(raw_features):
        try:
            if not isinstance(raw_feature, dict) or raw_feature.get("type") != "Feature":
                raise ValueError("item is not a GeoJSON Feature")
            properties = raw_feature.get("properties")
            if not isinstance(properties, dict):
                raise ValueError("properties must be an object")
            property_names.update(str(name) for name in properties)

            geometry, was_repaired, original_type = normalize_geometry(raw_feature.get("geometry"))
            geometry_types.add(original_type)
            repaired += int(was_repaired)

            # The upstream file is King County-wide despite its filename. Validate
            # every feature first, then deliberately retain only Seattle rows.
            if source.key == "neighborhoods" and _required_text(properties, "city") != "Seattle":
                excluded += 1
                continue

            record = normalize_properties(source, properties)
            record["geometry_geojson"] = json.dumps(mapping(geometry), separators=(",", ":"))
            record["source_properties"] = dict(properties)
            records.append(record)
        except (TypeError, ValueError) as exc:
            errors.append(f"feature[{index}]: {exc}")

    if errors:
        preview = "; ".join(errors[:10])
        if len(errors) > 10:
            preview += f"; ... {len(errors) - 10} additional errors"
        raise ValueError(
            f"{source.source_name} contains {len(errors)} invalid feature(s); "
            f"no rows loaded: {preview}"
        )
    if not records:
        raise ValueError(f"{source.source_name} produced no in-scope records")
    _assert_unique_business_keys(source, records)
    LOGGER.info(
        "Validated %s: received=%s, ready=%s, scope_excluded=%s, repaired=%s",
        source.source_name,
        len(raw_features),
        len(records),
        excluded,
        repaired,
    )
    return ProcessedSource(
        definition=source,
        records=records,
        features_received=len(raw_features),
        features_excluded=excluded,
        geometries_repaired=repaired,
        property_names=tuple(sorted(property_names)),
        geometry_types=tuple(sorted(geometry_types)),
    )


def normalize_geometry(raw_geometry: object) -> tuple[MultiPolygon, bool, str]:
    if not isinstance(raw_geometry, dict):
        raise ValueError("geometry must be an object")
    original_type = raw_geometry.get("type")
    if original_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"unsupported geometry type {original_type!r}")
    try:
        candidate = shape(raw_geometry)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"geometry could not be parsed: {exc}") from exc
    if candidate.is_empty:
        raise ValueError("geometry is empty")
    if not all(math.isfinite(value) for value in candidate.bounds):
        raise ValueError("geometry contains non-finite coordinates")
    min_x, min_y, max_x, max_y = candidate.bounds
    if min_x < -180 or max_x > 180 or min_y < -90 or max_y > 90:
        raise ValueError(f"geometry bounds are outside EPSG:4326: {candidate.bounds}")

    repaired = not candidate.is_valid
    if repaired:
        LOGGER.warning("Repairing invalid source geometry: %s", explain_validity(candidate))
        candidate = make_valid(candidate)
    polygons = list(_polygon_parts(candidate))
    if not polygons:
        raise ValueError("geometry repair produced no polygon components")
    normalized = MultiPolygon([orient(polygon, sign=1.0) for polygon in polygons])
    if not normalized.is_valid:
        repaired = True
        repaired_geometry = make_valid(normalized)
        polygons = list(_polygon_parts(repaired_geometry))
        normalized = MultiPolygon([orient(polygon, sign=1.0) for polygon in polygons])
    if normalized.is_empty or not normalized.is_valid:
        raise ValueError("geometry is still invalid after repair")
    return normalized, repaired, str(original_type)


def _polygon_parts(geometry: Geometry) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon | GeometryCollection):
        for part in geometry.geoms:
            yield from _polygon_parts(part)


def normalize_properties(
    source: SourceDefinition,
    properties: Mapping[str, Any],
) -> dict[str, Any]:
    if source.key == "neighborhoods":
        name = _required_text(properties, "name")
        nested = _optional_text(properties.get("nested"))
        return {
            "neighborhood_id": f"seattle-{_slug(name)}",
            "neighborhood_name": name,
            "parent_neighborhood_name": _optional_text(properties.get("nhood")),
            "is_nested": nested is not None,
            "source_area": _optional_float(properties.get("area")),
            "city_name": _required_text(properties, "city"),
            "county_name": _optional_text(properties.get("county")),
        }
    if source.key == "zip_codes":
        zip_code = _required_text(properties, "ZCTA5CE10")
        if not re.fullmatch(r"[0-9]{5}", zip_code):
            raise ValueError(f"ZCTA5CE10 must be five digits; received {zip_code!r}")
        return {
            "zip_code": zip_code,
            "census_geoid": _optional_text(properties.get("GEOID10")),
            "census_affgeoid": _optional_text(properties.get("AFFGEOID10")),
            "land_area_square_meters": _optional_int(properties.get("ALAND10")),
            "water_area_square_meters": _optional_int(properties.get("AWATER10")),
        }
    if source.key == "council_districts":
        district_id = _required_int(properties, "district")
        if district_id <= 0:
            raise ValueError("district must be positive")
        return {
            "council_district_id": district_id,
            "council_district_name": f"District {district_id}",
        }
    raise ValueError(f"unsupported source key {source.key!r}")


def load_dimensions(
    connection: Connection[Any],
    processed_sources: Sequence[ProcessedSource],
    *,
    grid_size_degrees: float,
) -> dict[str, int]:
    loaded_at = datetime.now(UTC)
    by_key = {source.definition.key: source for source in processed_sources}
    required_keys = {source.key for source in SOURCES}
    if set(by_key) != required_keys:
        raise ValueError(f"expected processed sources {sorted(required_keys)}")

    with connection.transaction():
        _upsert_neighborhoods(connection, by_key["neighborhoods"], loaded_at)
        _upsert_zip_areas(connection, by_key["zip_codes"], loaded_at)
        _upsert_council_districts(connection, by_key["council_districts"], loaded_at)
        grid_count = connection.execute(
            "select refresh_dim_grid(%s)", (grid_size_degrees,)
        ).fetchone()
        if grid_count is None:
            raise RuntimeError("refresh_dim_grid returned no result")
        verification = verify_database(connection)
        verification["dim_grid"] = int(grid_count[0])
    return verification


def _upsert_neighborhoods(
    connection: Connection[Any], source: ProcessedSource, loaded_at: datetime
) -> None:
    sql = """
        insert into dim_neighborhood (
            neighborhood_id, neighborhood_name, parent_neighborhood_name, is_nested,
            source_area, city_name, county_name, geometry, source_properties,
            source_name, source_url, loaded_at
        ) values (
            %(neighborhood_id)s, %(neighborhood_name)s, %(parent_neighborhood_name)s,
            %(is_nested)s, %(source_area)s, %(city_name)s, %(county_name)s,
            st_multi(st_geomfromgeojson(%(geometry_geojson)s))::geometry(MultiPolygon, 4326),
            %(source_properties)s, %(source_name)s, %(source_url)s, %(loaded_at)s
        )
        on conflict (neighborhood_id) do update set
            neighborhood_name = excluded.neighborhood_name,
            parent_neighborhood_name = excluded.parent_neighborhood_name,
            is_nested = excluded.is_nested,
            source_area = excluded.source_area,
            city_name = excluded.city_name,
            county_name = excluded.county_name,
            geometry = excluded.geometry,
            source_properties = excluded.source_properties,
            source_name = excluded.source_name,
            source_url = excluded.source_url,
            loaded_at = excluded.loaded_at
    """
    _execute_records(connection, source, loaded_at, sql)
    connection.execute(
        "delete from dim_neighborhood where source_url = %s and loaded_at <> %s",
        (source.definition.source_url, loaded_at),
    )


def _upsert_zip_areas(
    connection: Connection[Any], source: ProcessedSource, loaded_at: datetime
) -> None:
    sql = """
        insert into dim_zip_area (
            zip_code, census_geoid, census_affgeoid, land_area_square_meters,
            water_area_square_meters, geometry, source_properties,
            source_name, source_url, loaded_at
        ) values (
            %(zip_code)s, %(census_geoid)s, %(census_affgeoid)s,
            %(land_area_square_meters)s, %(water_area_square_meters)s,
            st_multi(st_geomfromgeojson(%(geometry_geojson)s))::geometry(MultiPolygon, 4326),
            %(source_properties)s, %(source_name)s, %(source_url)s, %(loaded_at)s
        )
        on conflict (zip_code) do update set
            census_geoid = excluded.census_geoid,
            census_affgeoid = excluded.census_affgeoid,
            land_area_square_meters = excluded.land_area_square_meters,
            water_area_square_meters = excluded.water_area_square_meters,
            geometry = excluded.geometry,
            source_properties = excluded.source_properties,
            source_name = excluded.source_name,
            source_url = excluded.source_url,
            loaded_at = excluded.loaded_at
    """
    _execute_records(connection, source, loaded_at, sql)
    connection.execute(
        "delete from dim_zip_area where source_url = %s and loaded_at <> %s",
        (source.definition.source_url, loaded_at),
    )


def _upsert_council_districts(
    connection: Connection[Any], source: ProcessedSource, loaded_at: datetime
) -> None:
    sql = """
        insert into dim_council_district (
            council_district_id, council_district_name, geometry, source_properties,
            source_name, source_url, loaded_at
        ) values (
            %(council_district_id)s, %(council_district_name)s,
            st_multi(st_geomfromgeojson(%(geometry_geojson)s))::geometry(MultiPolygon, 4326),
            %(source_properties)s, %(source_name)s, %(source_url)s, %(loaded_at)s
        )
        on conflict (council_district_id) do update set
            council_district_name = excluded.council_district_name,
            geometry = excluded.geometry,
            source_properties = excluded.source_properties,
            source_name = excluded.source_name,
            source_url = excluded.source_url,
            loaded_at = excluded.loaded_at
    """
    _execute_records(connection, source, loaded_at, sql)
    connection.execute(
        "delete from dim_council_district where source_url = %s and loaded_at <> %s",
        (source.definition.source_url, loaded_at),
    )


def _execute_records(
    connection: Connection[Any],
    source: ProcessedSource,
    loaded_at: datetime,
    sql: str,
) -> None:
    parameters = []
    for record in source.records:
        row = dict(record)
        row.update(
            source_properties=Jsonb(record["source_properties"]),
            source_name=source.definition.source_name,
            source_url=source.definition.source_url,
            loaded_at=loaded_at,
        )
        parameters.append(row)
    with connection.cursor() as cursor:
        cursor.executemany(sql, parameters)


def verify_database(connection: Connection[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "dim_neighborhood",
        "dim_zip_area",
        "dim_council_district",
        "dim_grid",
    ):
        row = connection.execute(
            f"""
            select
                count(*)::integer,
                count(*) filter (where not st_isvalid(geometry))::integer,
                count(*) filter (where st_srid(geometry) <> 4326)::integer
            from {table}
            """
        ).fetchone()
        if row is None:
            raise RuntimeError(f"verification returned no result for {table}")
        count, invalid_count, wrong_srid_count = (int(value) for value in row)
        if count == 0 or invalid_count != 0 or wrong_srid_count != 0:
            raise RuntimeError(
                f"{table} failed verification: rows={count}, invalid={invalid_count}, "
                f"wrong_srid={wrong_srid_count}"
            )
        counts[table] = count
        LOGGER.info(
            "Database geometry verified: table=%s rows=%s invalid=%s wrong_srid=%s",
            table,
            count,
            invalid_count,
            wrong_srid_count,
        )

    missing_indexes = connection.execute(
        """
        select expected.index_name
        from unnest(array[
            'idx_dim_neighborhood_geometry',
            'idx_dim_zip_area_geometry',
            'idx_dim_council_district_geometry',
            'idx_dim_grid_geometry'
        ]) as expected(index_name)
        where not exists (
            select 1 from pg_indexes
            where schemaname = 'public' and indexname = expected.index_name
        )
        """
    ).fetchall()
    if missing_indexes:
        raise RuntimeError(f"missing spatial indexes: {[row[0] for row in missing_indexes]}")
    LOGGER.info("Verified all four GiST spatial indexes")

    sample = connection.execute(
        "select * from lookup_vehicle_geography(%s, %s)", (-122.3321, 47.6062)
    ).fetchone()
    if sample is None or not any(sample):
        raise RuntimeError("downtown Seattle sample point did not match any geography")
    LOGGER.info(
        "Seattle sample verified: longitude=-122.3321 latitude=47.6062 result=%s",
        tuple(sample),
    )
    outside = connection.execute(
        "select * from lookup_vehicle_geography(%s, %s)", (-74.0060, 40.7128)
    ).fetchone()
    if outside is None or any(value is not None for value in outside):
        raise RuntimeError("outside-Seattle sample point unexpectedly matched geography")
    LOGGER.info("Outside-Seattle sample verified: all geography values are null")

    enriched_sample = connection.execute(
        """
        select
            vehicle_id,
            neighborhood_name,
            zip_code,
            council_district_name,
            grid_id
        from vw_vehicle_geography_enriched
        order by snapshot_timestamp desc, vehicle_id
        limit 1
        """
    ).fetchone()
    if enriched_sample is None:
        LOGGER.info("Enriched view verified but contains no vehicle snapshots yet")
    else:
        LOGGER.info("Enriched vehicle view verified: sample=%s", tuple(enriched_sample))
    return counts


def _assert_unique_business_keys(
    source: SourceDefinition, records: Sequence[Mapping[str, Any]]
) -> None:
    key_name = {
        "neighborhoods": "neighborhood_id",
        "zip_codes": "zip_code",
        "council_districts": "council_district_id",
    }[source.key]
    values = [record[key_name] for record in records]
    duplicates = sorted({value for value in values if values.count(value) > 1}, key=str)
    if duplicates:
        raise ValueError(f"{source.source_name} has duplicate {key_name} values: {duplicates}")


def _required_text(properties: Mapping[str, Any], name: str) -> str:
    value = _optional_text(properties.get(name))
    if value is None:
        raise ValueError(f"{name} is required")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_int(properties: Mapping[str, Any], name: str) -> int:
    value = _optional_int(properties.get(name))
    if value is None:
        raise ValueError(f"{name} is required")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"expected integer, received {value!r}")
    numeric = float(cast(Any, value))
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"expected integer, received {value!r}")
    return int(numeric)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    numeric = float(cast(Any, value))
    if not math.isfinite(numeric):
        raise ValueError(f"expected finite number, received {value!r}")
    return numeric


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def _grid_size_from_environment() -> float:
    import os

    raw = os.getenv("GEOGRAPHY_GRID_SIZE_DEGREES") or os.getenv("GRID_SIZE_DEGREES") or "0.01"
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("GEOGRAPHY_GRID_SIZE_DEGREES must be a number") from exc
    if not 0 < value <= 180:
        raise ValueError("GEOGRAPHY_GRID_SIZE_DEGREES must be greater than 0 and at most 180")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="download and validate all sources without connecting to PostgreSQL",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        load_environment()
        grid_size = _grid_size_from_environment()
        with build_http_session() as session:
            processed = [
                process_source(source, download_geojson(session, source)) for source in SOURCES
            ]
        for source in processed:
            LOGGER.info(
                "%s schema: properties=%s geometry_types=%s",
                source.definition.key,
                ",".join(source.property_names),
                ",".join(source.geometry_types),
            )
        if args.dry_run:
            LOGGER.info("Dry run complete; PostgreSQL was not modified")
            return 0

        database = load_database_settings()
        with psycopg.connect(
            host=database.host,
            port=database.port,
            dbname=database.name,
            user=database.user,
            password=database.password,
            sslmode=database.sslmode,
            connect_timeout=database.connect_timeout_seconds,
            options=f"-c statement_timeout={database.statement_timeout_seconds * 1000}",
        ) as connection:
            counts = load_dimensions(connection, processed, grid_size_degrees=grid_size)
        LOGGER.info(
            "Geography load committed: %s",
            ", ".join(f"{table}={count}" for table, count in sorted(counts.items())),
        )
        return 0
    except Exception:
        LOGGER.exception("Geography ingestion failed; no partial load was committed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
