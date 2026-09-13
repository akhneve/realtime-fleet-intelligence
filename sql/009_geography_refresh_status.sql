-- Exposes the stored loaded_at timestamps as a small monitoring dataset.
-- loaded_at changes only after a complete, successful geography transaction.
create or replace view vw_geography_refresh_status as
select
    'dim_neighborhood'::text as relation_name,
    source_name,
    source_url,
    count(*)::integer as row_count,
    min(loaded_at) as earliest_row_update,
    max(loaded_at) as last_successful_update,
    now() - max(loaded_at) as time_since_last_successful_update
from dim_neighborhood
group by source_name, source_url
union all
select
    'dim_zip_area',
    source_name,
    source_url,
    count(*)::integer,
    min(loaded_at),
    max(loaded_at),
    now() - max(loaded_at)
from dim_zip_area
group by source_name, source_url
union all
select
    'dim_council_district',
    source_name,
    source_url,
    count(*)::integer,
    min(loaded_at),
    max(loaded_at),
    now() - max(loaded_at)
from dim_council_district
group by source_name, source_url
union all
select
    'dim_grid',
    source_name,
    source_url,
    count(*)::integer,
    min(loaded_at),
    max(loaded_at),
    now() - max(loaded_at)
from dim_grid
group by source_name, source_url;

revoke all privileges on vw_geography_refresh_status from public, anon, authenticated;
grant select on vw_geography_refresh_status to fleet_reporting;
