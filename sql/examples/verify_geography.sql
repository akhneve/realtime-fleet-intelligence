-- Row counts, geometry validity, and SRID contract.
select
    'dim_neighborhood' as relation_name,
    count(*) as row_count,
    count(*) filter (where not st_isvalid(geometry)) as invalid_geometry_count,
    count(*) filter (where st_srid(geometry) <> 4326) as wrong_srid_count
from dim_neighborhood
union all
select
    'dim_zip_area',
    count(*),
    count(*) filter (where not st_isvalid(geometry)),
    count(*) filter (where st_srid(geometry) <> 4326)
from dim_zip_area
union all
select
    'dim_council_district',
    count(*),
    count(*) filter (where not st_isvalid(geometry)),
    count(*) filter (where st_srid(geometry) <> 4326)
from dim_council_district
union all
select
    'dim_grid',
    count(*),
    count(*) filter (where not st_isvalid(geometry)),
    count(*) filter (where st_srid(geometry) <> 4326)
from dim_grid
order by relation_name;

-- All four rows should report a GiST index.
select tablename, indexname, indexdef
from pg_indexes
where schemaname = 'public'
  and indexname in (
      'idx_dim_neighborhood_geometry',
      'idx_dim_zip_area_geometry',
      'idx_dim_council_district_geometry',
      'idx_dim_grid_geometry'
  )
order by tablename;

-- Expected checked result: Central Business District, 98164, District 7,
-- GRID_13760_05766.
select * from lookup_vehicle_geography(-122.3321, 47.6062);

-- Expected result: one row with four NULL values.
select * from lookup_vehicle_geography(-74.0060, 40.7128);

-- Stored last-success timestamps for monthly refresh monitoring.
select
    relation_name,
    row_count,
    last_successful_update,
    time_since_last_successful_update
from vw_geography_refresh_status
order by relation_name;

-- Lateral lookup must not duplicate vehicle-grain facts.
select
    (select count(*) from fact_vehicle_snapshot) as fact_row_count,
    (select count(*) from vw_vehicle_geography_enriched) as enriched_view_row_count;

-- Inspect populated and null-enriched examples through the Power BI contract.
select *
from vw_vehicle_geography_enriched
where neighborhood_name is not null
order by snapshot_timestamp desc
limit 10;

select *
from vw_vehicle_geography_enriched
where neighborhood_name is null
order by snapshot_timestamp desc
limit 10;
