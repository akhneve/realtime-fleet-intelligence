-- Vehicle-level Power BI source. Use the four geography columns as slicers.
select
    vehicle_id,
    snapshot_timestamp,
    latitude,
    longitude,
    available_flag,
    neighborhood_name,
    zip_code,
    council_district_name,
    grid_id,
    source_timestamp,
    ingestion_timestamp
from vw_vehicle_geography_enriched;

-- Compact import-mode dataset for availability visuals by geography.
select
    snapshot_timestamp,
    neighborhood_name,
    zip_code,
    council_district_name,
    grid_id,
    count(*)::integer as total_vehicles,
    count(*) filter (where available_flag)::integer as available_vehicles,
    count(*) filter (where not available_flag)::integer as unavailable_vehicles,
    count(*) filter (where available_flag)::numeric / nullif(count(*), 0)
        as availability_rate
from vw_vehicle_geography_enriched
group by
    snapshot_timestamp,
    neighborhood_name,
    zip_code,
    council_district_name,
    grid_id;

-- Optional slicer values, excluding null geography for cleaner filter lists.
select distinct neighborhood_name
from vw_vehicle_geography_enriched
where neighborhood_name is not null
order by neighborhood_name;

select distinct zip_code
from vw_vehicle_geography_enriched
where zip_code is not null
order by zip_code;

select distinct council_district_name
from vw_vehicle_geography_enriched
where council_district_name is not null
order by council_district_name;

select distinct grid_id
from vw_vehicle_geography_enriched
where grid_id is not null
order by grid_id;
