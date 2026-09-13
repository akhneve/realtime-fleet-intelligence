from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sql(name: str) -> str:
    return (PROJECT_ROOT / "sql" / name).read_text(encoding="utf-8").lower()


def test_migrations_are_additive_and_ordered() -> None:
    names = sorted(path.name for path in (PROJECT_ROOT / "sql").glob("*.sql"))
    assert names == [
        "001_schema.sql",
        "002_indexes.sql",
        "003_views.sql",
        "004_security.sql",
        "005_production_hardening.sql",
        "006_least_privilege_roles.sql",
        "007_all_lime_gbfs_feeds.sql",
        "008_geography_enrichment.sql",
        "009_geography_refresh_status.sql",
    ]


def test_hardening_adds_constraints_and_snapshot_context() -> None:
    migration = sql("005_production_hardening.sql")
    assert "not valid" in migration
    assert "validate constraint" in migration
    assert "snapshot_context as" in migration
    assert "max(source_timestamp)" in migration
    assert "baseline_sample_count" in migration
    assert "baseline_ready" in migration
    assert "count(*) >= 4" in migration


def test_least_privilege_roles_and_rls_policy_exist() -> None:
    migration = sql("006_least_privilege_roles.sql")
    assert "create role fleet_ingest nologin" in migration
    assert "create role fleet_reporting nologin" in migration
    assert "revoke create on schema public from public" in migration
    assert "create policy fleet_ingest_all" in migration
    assert "grant select on" in migration


def test_all_lime_feeds_have_storage_views_and_security() -> None:
    migration = sql("007_all_lime_gbfs_feeds.sql")
    for relation in (
        "system_information",
        "station_information",
        "current_station_status",
        "fact_station_status_snapshot",
        "feed_run_metrics",
    ):
        assert relation in migration
    assert "vw_current_station_supply" in migration
    assert "vw_feed_health" in migration
    assert "enable row level security" in migration
    assert "deferrable initially deferred" in migration


def test_geography_migration_defines_spatial_contract() -> None:
    migration = sql("008_geography_enrichment.sql")
    for relation in (
        "dim_neighborhood",
        "dim_zip_area",
        "dim_council_district",
        "dim_grid",
        "vw_vehicle_geography_enriched",
    ):
        assert relation in migration
    assert "create extension if not exists postgis" in migration
    assert migration.count("using gist (geometry)") == 4
    assert "geometry(multipolygon, 4326)" in migration
    assert "lookup_vehicle_geography" in migration
    assert "st_covers" in migration
    assert "refresh_dim_grid" in migration


def test_geography_refresh_status_exposes_stored_update_times() -> None:
    migration = sql("009_geography_refresh_status.sql")
    assert "vw_geography_refresh_status" in migration
    assert "last_successful_update" in migration
    assert migration.count("max(loaded_at)") == 8
    assert "grant select on vw_geography_refresh_status to fleet_reporting" in migration
