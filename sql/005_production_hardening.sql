-- Additive integrity checks for existing installations. NOT VALID avoids taking a
-- long validation lock while each constraint is installed; validation follows.
do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'current_vehicle_state_coordinates_chk') then
        alter table current_vehicle_state add constraint current_vehicle_state_coordinates_chk
            check (latitude between -90 and 90 and longitude between -180 and 180) not valid;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'fact_vehicle_snapshot_coordinates_chk') then
        alter table fact_vehicle_snapshot add constraint fact_vehicle_snapshot_coordinates_chk
            check (latitude between -90 and 90 and longitude between -180 and 180) not valid;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'fact_vehicle_snapshot_quality_chk') then
        alter table fact_vehicle_snapshot add constraint fact_vehicle_snapshot_quality_chk
            check (quality_flag in ('SUCCESS', 'WARNING', 'FAILED')) not valid;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'fact_grid_15min_counts_chk') then
        alter table fact_grid_15min add constraint fact_grid_15min_counts_chk
            check (
                total_vehicles >= 0
                and available_vehicles >= 0
                and unavailable_vehicles >= 0
                and total_vehicles = available_vehicles + unavailable_vehicles
            ) not valid;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'pipeline_runs_metrics_chk') then
        alter table pipeline_runs add constraint pipeline_runs_metrics_chk
            check (
                records_received >= 0
                and records_valid >= 0
                and records_rejected >= 0
                and pipeline_duration_ms >= 0
                and (api_latency_ms is null or api_latency_ms >= 0)
                and records_received = records_valid + records_rejected
            ) not valid;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'reporting_thresholds_value_chk') then
        alter table reporting_thresholds add constraint reporting_thresholds_value_chk
            check (setting_value >= 0) not valid;
    end if;
end
$$;

alter table current_vehicle_state validate constraint current_vehicle_state_coordinates_chk;
alter table fact_vehicle_snapshot validate constraint fact_vehicle_snapshot_coordinates_chk;
alter table fact_vehicle_snapshot validate constraint fact_vehicle_snapshot_quality_chk;
alter table fact_grid_15min validate constraint fact_grid_15min_counts_chk;
alter table pipeline_runs validate constraint pipeline_runs_metrics_chk;
alter table reporting_thresholds validate constraint reporting_thresholds_value_chk;

-- Baseline columns are appended so existing Power BI field contracts remain intact.
create or replace view vw_grid_supply_baseline as
select
    grid_id,
    extract(dow from bucket_timestamp at time zone 'America/Los_Angeles')::integer as day_of_week,
    (date_part('hour', bucket_timestamp at time zone 'America/Los_Angeles')::integer * 60
        + date_part('minute', bucket_timestamp at time zone 'America/Los_Angeles')::integer) / 15 as bucket_15min_index,
    avg(available_vehicles)::numeric(12, 2) as avg_available_vehicles,
    percentile_cont(0.5) within group (order by available_vehicles) as median_available_vehicles,
    percentile_cont(0.25) within group (order by available_vehicles) as p25_available_vehicles,
    percentile_cont(0.75) within group (order by available_vehicles) as p75_available_vehicles,
    count(*)::integer as baseline_sample_count,
    count(*) >= 4 as baseline_ready
from fact_grid_15min
group by grid_id, day_of_week, bucket_15min_index;

create or replace view vw_rebalancing_priority as
with current_supply as (
    select * from vw_current_grid_supply
),
snapshot_context as (
    -- Current state can be hours old; score it against its own local time bucket.
    select coalesce(max(source_timestamp), max(ingestion_timestamp), now()) as context_timestamp
    from current_vehicle_state
),
latest_context as (
    select
        extract(dow from context_timestamp at time zone 'America/Los_Angeles')::integer as day_of_week,
        (date_part('hour', context_timestamp at time zone 'America/Los_Angeles')::integer * 60
            + date_part('minute', context_timestamp at time zone 'America/Los_Angeles')::integer) / 15 as bucket_15min_index
    from snapshot_context
),
context_baseline as (
    select baseline.*
    from vw_grid_supply_baseline baseline
    cross join latest_context context
    where baseline.day_of_week = context.day_of_week
      and baseline.bucket_15min_index = context.bucket_15min_index
),
thresholds as (
    select
        coalesce(max(setting_value) filter (where setting_name = 'rebalance_high_threshold'), 10) as high_threshold,
        coalesce(max(setting_value) filter (where setting_name = 'rebalance_medium_threshold'), 5) as medium_threshold
    from reporting_thresholds
),
candidate_grids as (
    select grid_id from current_supply
    union
    select grid_id from context_baseline
)
select
    grids.grid_id,
    coalesce(current.available_vehicles, 0) as current_available_vehicles,
    baseline.avg_available_vehicles as historical_typical_available_vehicles,
    coalesce(baseline.avg_available_vehicles, 0) - coalesce(current.available_vehicles, 0) as supply_gap,
    coalesce(current.availability_rate, 0) as availability_rate,
    case
        when not coalesce(baseline.baseline_ready, false) then 'LOW'
        when coalesce(baseline.avg_available_vehicles, 0) - coalesce(current.available_vehicles, 0)
            >= thresholds.high_threshold then 'HIGH'
        when coalesce(baseline.avg_available_vehicles, 0) - coalesce(current.available_vehicles, 0)
            >= thresholds.medium_threshold then 'MEDIUM'
        else 'LOW'
    end as priority,
    'Suggested operational rebalancing signal based on observed availability patterns.' as interpretation,
    coalesce(baseline.baseline_sample_count, 0) as baseline_sample_count,
    coalesce(baseline.baseline_ready, false) as baseline_ready
from candidate_grids grids
cross join thresholds
left join current_supply current on current.grid_id = grids.grid_id
left join context_baseline baseline on baseline.grid_id = grids.grid_id;
