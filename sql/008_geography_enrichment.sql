-- Adds authoritative Seattle boundary dimensions and a reusable point-in-polygon layer.
-- Run this migration as the schema owner; runtime roles cannot install extensions.
create extension if not exists postgis;

-- Supabase commonly exposes extensions through this search path, while vanilla
-- PostgreSQL installs PostGIS in public. Including both supports either layout.
set search_path = public, extensions;

create table if not exists dim_neighborhood (
    neighborhood_id text primary key,
    neighborhood_name text not null,
    parent_neighborhood_name text,
    is_nested boolean not null,
    source_area numeric,
    city_name text not null,
    county_name text,
    geometry geometry(MultiPolygon, 4326) not null,
    source_properties jsonb not null default '{}'::jsonb,
    source_name text not null,
    source_url text not null,
    loaded_at timestamptz not null,
    constraint dim_neighborhood_name_chk check (btrim(neighborhood_name) <> ''),
    constraint dim_neighborhood_city_chk check (city_name = 'Seattle'),
    constraint dim_neighborhood_geometry_valid_chk check (st_isvalid(geometry))
);

create table if not exists dim_zip_area (
    zip_code text primary key,
    census_geoid text,
    census_affgeoid text,
    land_area_square_meters bigint,
    water_area_square_meters bigint,
    geometry geometry(MultiPolygon, 4326) not null,
    source_properties jsonb not null default '{}'::jsonb,
    source_name text not null,
    source_url text not null,
    loaded_at timestamptz not null,
    constraint dim_zip_area_code_chk check (zip_code ~ '^[0-9]{5}$'),
    constraint dim_zip_area_geometry_valid_chk check (st_isvalid(geometry))
);

create table if not exists dim_council_district (
    council_district_id integer primary key,
    council_district_name text not null unique,
    geometry geometry(MultiPolygon, 4326) not null,
    source_properties jsonb not null default '{}'::jsonb,
    source_name text not null,
    source_url text not null,
    loaded_at timestamptz not null,
    constraint dim_council_district_id_chk check (council_district_id > 0),
    constraint dim_council_district_geometry_valid_chk check (st_isvalid(geometry))
);

-- Each geometry is a degree-based operational grid cell clipped to Seattle's
-- council-district union. Its ID matches fleet_intelligence.transform.assign_grid_id.
create table if not exists dim_grid (
    grid_id text primary key,
    grid_size_degrees double precision not null,
    min_latitude double precision not null,
    max_latitude double precision not null,
    min_longitude double precision not null,
    max_longitude double precision not null,
    geometry geometry(MultiPolygon, 4326) not null,
    source_name text not null,
    source_url text not null,
    loaded_at timestamptz not null,
    constraint dim_grid_id_chk check (grid_id ~ '^GRID_[0-9]+_[0-9]+$'),
    constraint dim_grid_size_chk check (grid_size_degrees > 0 and grid_size_degrees <= 180),
    constraint dim_grid_latitude_chk check (
        min_latitude >= -90 and max_latitude <= 90 and min_latitude < max_latitude
    ),
    constraint dim_grid_longitude_chk check (
        min_longitude >= -180 and max_longitude <= 180 and min_longitude < max_longitude
    ),
    constraint dim_grid_geometry_valid_chk check (st_isvalid(geometry))
);

create index if not exists idx_dim_neighborhood_geometry
    on dim_neighborhood using gist (geometry);
create index if not exists idx_dim_zip_area_geometry
    on dim_zip_area using gist (geometry);
create index if not exists idx_dim_council_district_geometry
    on dim_council_district using gist (geometry);
create index if not exists idx_dim_grid_geometry
    on dim_grid using gist (geometry);

create index if not exists idx_dim_neighborhood_name
    on dim_neighborhood (neighborhood_name);
create index if not exists idx_dim_zip_area_code
    on dim_zip_area (zip_code);
create index if not exists idx_dim_council_district_name
    on dim_council_district (council_district_name);

-- Rebuilds the grid from the current Seattle council boundary. Full degree cells
-- are clipped at the city edge, which guarantees out-of-city points do not match.
create or replace function refresh_dim_grid(p_grid_size_degrees double precision default 0.01)
returns integer
language plpgsql
as $$
declare
    v_loaded_at timestamptz := clock_timestamp();
    v_row_count integer;
begin
    if p_grid_size_degrees <= 0 or p_grid_size_degrees > 180 then
        raise exception 'grid size must be greater than 0 and at most 180';
    end if;
    if not exists (select 1 from dim_council_district) then
        raise exception 'dim_council_district must be loaded before dim_grid can be refreshed';
    end if;

    with city_boundary as (
        select st_unaryunion(st_collect(geometry)) as geometry
        from dim_council_district
    ),
    bucket_extent as (
        select
            floor((st_ymin(st_extent(geometry)) + 90) / p_grid_size_degrees)::integer
                as min_lat_bucket,
            floor((st_ymax(st_extent(geometry)) + 90) / p_grid_size_degrees)::integer
                as max_lat_bucket,
            floor((st_xmin(st_extent(geometry)) + 180) / p_grid_size_degrees)::integer
                as min_lon_bucket,
            floor((st_xmax(st_extent(geometry)) + 180) / p_grid_size_degrees)::integer
                as max_lon_bucket
        from dim_council_district
    ),
    buckets as (
        select lat_bucket, lon_bucket
        from bucket_extent
        cross join lateral generate_series(min_lat_bucket, max_lat_bucket) as lat_bucket
        cross join lateral generate_series(min_lon_bucket, max_lon_bucket) as lon_bucket
    ),
    cells as (
        select
            lat_bucket,
            lon_bucket,
            lat_bucket * p_grid_size_degrees - 90 as min_latitude,
            (lat_bucket + 1) * p_grid_size_degrees - 90 as max_latitude,
            lon_bucket * p_grid_size_degrees - 180 as min_longitude,
            (lon_bucket + 1) * p_grid_size_degrees - 180 as max_longitude,
            st_makeenvelope(
                lon_bucket * p_grid_size_degrees - 180,
                lat_bucket * p_grid_size_degrees - 90,
                (lon_bucket + 1) * p_grid_size_degrees - 180,
                (lat_bucket + 1) * p_grid_size_degrees - 90,
                4326
            ) as geometry
        from buckets
    ),
    clipped as (
        select
            cells.*,
            st_multi(
                st_collectionextract(
                    st_intersection(cells.geometry, city_boundary.geometry),
                    3
                )
            )::geometry(MultiPolygon, 4326) as clipped_geometry
        from cells
        cross join city_boundary
        where st_intersects(cells.geometry, city_boundary.geometry)
    )
    insert into dim_grid (
        grid_id,
        grid_size_degrees,
        min_latitude,
        max_latitude,
        min_longitude,
        max_longitude,
        geometry,
        source_name,
        source_url,
        loaded_at
    )
    select
        'GRID_' || lpad(lat_bucket::text, 5, '0') || '_' || lpad(lon_bucket::text, 5, '0'),
        p_grid_size_degrees,
        min_latitude,
        max_latitude,
        min_longitude,
        max_longitude,
        clipped_geometry,
        'Derived from Seattle city council districts',
        'https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/master/data/city-council-districts.geojson',
        v_loaded_at
    from clipped
    where not st_isempty(clipped_geometry)
    on conflict (grid_id) do update set
        grid_size_degrees = excluded.grid_size_degrees,
        min_latitude = excluded.min_latitude,
        max_latitude = excluded.max_latitude,
        min_longitude = excluded.min_longitude,
        max_longitude = excluded.max_longitude,
        geometry = excluded.geometry,
        source_name = excluded.source_name,
        source_url = excluded.source_url,
        loaded_at = excluded.loaded_at;

    delete from dim_grid where loaded_at <> v_loaded_at;
    select count(*)::integer into v_row_count from dim_grid;
    return v_row_count;
end
$$;

-- Resolves one deterministic geography row per point. Neighborhoods are ordered
-- by smallest area so a detailed polygon wins if source polygons overlap.
create or replace function lookup_vehicle_geography(
    p_longitude double precision,
    p_latitude double precision
)
returns table (
    neighborhood_name text,
    zip_code text,
    council_district_name text,
    grid_id text
)
language sql
stable
parallel safe
as $$
    with point_input as (
        select st_setsrid(st_makepoint(p_longitude, p_latitude), 4326) as geometry
        where p_latitude between -90 and 90
          and p_longitude between -180 and 180
    )
    select
        neighborhood.neighborhood_name,
        zip_area.zip_code,
        council.council_district_name,
        grid.grid_id
    from point_input point
    left join lateral (
        select district.council_district_id, district.council_district_name
        from dim_council_district district
        where st_covers(district.geometry, point.geometry)
        order by district.council_district_id
        limit 1
    ) council on true
    left join lateral (
        select candidate.neighborhood_name
        from dim_neighborhood candidate
        where council.council_district_id is not null
          and st_covers(candidate.geometry, point.geometry)
        order by st_area(candidate.geometry::geography), candidate.neighborhood_id
        limit 1
    ) neighborhood on true
    left join lateral (
        select candidate.zip_code
        from dim_zip_area candidate
        where council.council_district_id is not null
          and st_covers(candidate.geometry, point.geometry)
        order by st_area(candidate.geometry::geography), candidate.zip_code
        limit 1
    ) zip_area on true
    left join lateral (
        select candidate.grid_id
        from dim_grid candidate
        where council.council_district_id is not null
          and st_covers(candidate.geometry, point.geometry)
        order by candidate.grid_id
        limit 1
    ) grid on true
$$;

create or replace view vw_vehicle_geography_enriched as
select
    vehicle.vehicle_id,
    vehicle.snapshot_timestamp,
    vehicle.latitude,
    vehicle.longitude,
    vehicle.available_flag,
    geography.neighborhood_name,
    geography.zip_code,
    geography.council_district_name,
    geography.grid_id,
    coalesce(feed.source_timestamp, vehicle.snapshot_timestamp) as source_timestamp,
    coalesce(run.completed_at, vehicle.snapshot_timestamp) as ingestion_timestamp
from fact_vehicle_snapshot vehicle
left join feed_run_metrics feed
    on feed.run_id = vehicle.run_id
   and feed.feed_name = 'free_bike_status'
left join pipeline_runs run on run.run_id = vehicle.run_id
left join lateral lookup_vehicle_geography(vehicle.longitude, vehicle.latitude) geography
    on true;

-- Match the project's existing least-privilege/RLS model.
revoke all privileges on dim_neighborhood, dim_zip_area, dim_council_district, dim_grid
    from public, anon, authenticated;
revoke all on function refresh_dim_grid(double precision) from public;
revoke all on function lookup_vehicle_geography(double precision, double precision) from public;

grant select, insert, update, delete on
    dim_neighborhood, dim_zip_area, dim_council_district, dim_grid
to fleet_ingest;
grant execute on function refresh_dim_grid(double precision) to fleet_ingest;

grant select on
    dim_neighborhood, dim_zip_area, dim_council_district, dim_grid,
    vw_vehicle_geography_enriched
to fleet_reporting;
grant execute on function lookup_vehicle_geography(double precision, double precision)
to fleet_reporting;

alter table dim_neighborhood enable row level security;
alter table dim_zip_area enable row level security;
alter table dim_council_district enable row level security;
alter table dim_grid enable row level security;

do $$
declare
    relation_name text;
begin
    foreach relation_name in array array[
        'dim_neighborhood',
        'dim_zip_area',
        'dim_council_district',
        'dim_grid'
    ]
    loop
        if not exists (
            select 1
            from pg_policies
            where schemaname = 'public'
              and tablename = relation_name
              and policyname = 'fleet_ingest_all'
        ) then
            execute format(
                'create policy fleet_ingest_all on %I for all to fleet_ingest using (true) with check (true)',
                relation_name
            );
        end if;
    end loop;
end
$$;

do $$
declare
    relation_name text;
begin
    foreach relation_name in array array[
        'dim_neighborhood',
        'dim_zip_area',
        'dim_council_district',
        'dim_grid'
    ]
    loop
        if not exists (
            select 1
            from pg_policies
            where schemaname = 'public'
              and tablename = relation_name
              and policyname = 'fleet_reporting_select'
        ) then
            execute format(
                'create policy fleet_reporting_select on %I for select to fleet_reporting using (true)',
                relation_name
            );
        end if;
    end loop;
end
$$;
