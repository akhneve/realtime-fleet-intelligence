-- Summarizes the latest live fleet state into dashboard KPI values.
-- Power BI should consume this view instead of recalculating core operational metrics from raw rows.
create or replace view vw_current_fleet_summary as
select
    count(*)::integer as current_vehicle_count, -- Counts all vehicles currently known to the platform.
    count(*) filter (where available_flag)::integer as available_vehicles, -- Counts vehicles ready for use based on ETL availability logic.
    count(*) filter (where not available_flag)::integer as unavailable_vehicles, -- Counts vehicles unavailable because reserved or disabled.
    max(source_timestamp) as latest_source_timestamp, -- Shows the newest source feed timestamp represented in current state.
    (select max(completed_at) from pipeline_runs where quality_status in ('SUCCESS', 'WARNING')) as latest_successful_ingestion, -- Shows the latest usable pipeline completion time.
    (select quality_status from pipeline_runs order by started_at desc limit 1) as pipeline_status -- Shows the most recent run status, including failures.
from current_vehicle_state;

-- Aggregates current vehicle state by geographic grid.
-- This powers latest-snapshot supply maps, top/bottom grid tables, and availability-rate visuals.
create or replace view vw_current_grid_supply as
select
    grid_id, -- Rectangular geographic bucket assigned by the Python transform step.
    count(*)::integer as current_total_vehicles, -- Total current vehicles observed in this grid.
    count(*) filter (where available_flag)::integer as available_vehicles, -- Current available supply in this grid.
    count(*) filter (where not available_flag)::integer as unavailable_vehicles, -- Current unavailable supply in this grid.
    count(*) filter (where available_flag)::numeric / nullif(count(*), 0) as availability_rate -- Availability share; nullif avoids divide-by-zero errors.
from current_vehicle_state
group by grid_id; -- One output row per grid so BI visuals stay compact.

-- Builds historical availability baselines by grid, day of week, and 15-minute time slot.
-- This gives the rebalancing view a typical-supply comparison without claiming demand prediction.
create or replace view vw_grid_supply_baseline as
select
    grid_id, -- Grid being benchmarked.
    extract(dow from bucket_timestamp at time zone 'America/Los_Angeles')::integer as day_of_week, -- Uses Seattle local time so weekday/weekend patterns match operations.
    (date_part('hour', bucket_timestamp at time zone 'America/Los_Angeles')::integer * 60
        + date_part('minute', bucket_timestamp at time zone 'America/Los_Angeles')::integer) / 15 as bucket_15min_index, -- Converts Seattle-local time of day into a stable 0-95 bucket index.
    avg(available_vehicles)::numeric(12, 2) as avg_available_vehicles, -- Mean available supply used as the initial baseline metric.
    percentile_cont(0.5) within group (order by available_vehicles) as median_available_vehicles, -- Median provides a robust comparison when supply has outliers.
    percentile_cont(0.25) within group (order by available_vehicles) as p25_available_vehicles, -- Lower quartile helps understand normal low-supply ranges.
    percentile_cont(0.75) within group (order by available_vehicles) as p75_available_vehicles -- Upper quartile helps understand normal high-supply ranges.
from fact_grid_15min
group by grid_id, day_of_week, bucket_15min_index; -- Groups by comparable location and time context.

-- Compares current grid supply against the historical baseline and assigns an operational priority.
-- This is an availability-based rebalancing signal, not a demand prediction model.
create or replace view vw_rebalancing_priority as
with current_supply as (
    -- Starts from the current grid supply view so priority logic reuses the same latest-snapshot definitions as the dashboard.
    select * from vw_current_grid_supply
),
latest_context as (
    -- Captures the current day/time bucket so current supply is compared to the right historical context.
    select
        extract(dow from now() at time zone 'America/Los_Angeles')::integer as day_of_week,
        (date_part('hour', now() at time zone 'America/Los_Angeles')::integer * 60
            + date_part('minute', now() at time zone 'America/Los_Angeles')::integer) / 15 as bucket_15min_index
),
context_baseline as (
    -- Narrows the historical baseline to the current Seattle-local weekday and time bucket.
    select b.*
    from vw_grid_supply_baseline b
    cross join latest_context lc
    where b.day_of_week = lc.day_of_week
      and b.bucket_15min_index = lc.bucket_15min_index
),
thresholds as (
    -- Pulls priority thresholds from a table so users can tune cutoffs without editing SQL view code.
    select
        coalesce(max(setting_value) filter (where setting_name = 'rebalance_high_threshold'), 10) as high_threshold,
        coalesce(max(setting_value) filter (where setting_name = 'rebalance_medium_threshold'), 5) as medium_threshold
    from reporting_thresholds
),
candidate_grids as (
    -- Includes historically active grids even when their current supply is zero.
    select grid_id from current_supply
    union
    select grid_id from context_baseline
)
select
    g.grid_id, -- Grid being scored for rebalancing priority.
    coalesce(c.available_vehicles, 0) as current_available_vehicles, -- Missing current grids represent zero observed supply.
    b.avg_available_vehicles as historical_typical_available_vehicles, -- Typical available supply for this grid and time bucket.
    coalesce(b.avg_available_vehicles, 0) - coalesce(c.available_vehicles, 0) as supply_gap, -- Positive values mean current supply is below baseline.
    coalesce(c.availability_rate, 0) as availability_rate, -- A historically active grid with no current vehicles has zero availability.
    case
        when coalesce(b.avg_available_vehicles, 0) - coalesce(c.available_vehicles, 0) >= t.high_threshold then 'HIGH' -- Flags large below-baseline gaps for immediate attention.
        when coalesce(b.avg_available_vehicles, 0) - coalesce(c.available_vehicles, 0) >= t.medium_threshold then 'MEDIUM' -- Flags moderate below-baseline gaps for review.
        else 'LOW' -- Keeps all other grids visible without overstating urgency.
    end as priority,
    'Suggested operational rebalancing signal based on observed availability patterns.' as interpretation -- Prevents the dashboard from presenting this as demand prediction.
from candidate_grids g
cross join thresholds t -- Adds the single threshold row to every current grid.
left join current_supply c on c.grid_id = g.grid_id
left join context_baseline b on b.grid_id = g.grid_id;

-- Exposes compact historical supply trends for Power BI.
-- This keeps trend visuals fast by reading pre-aggregated 15-minute grid metrics.
create or replace view vw_fleet_availability_trend as
select
    bucket_timestamp, -- 15-minute reporting bucket.
    grid_id, -- Geographic bucket for filtering and drill-down.
    total_vehicles, -- Total observed vehicles in the bucket/grid.
    available_vehicles, -- Available observed vehicles in the bucket/grid.
    unavailable_vehicles, -- Unavailable observed vehicles in the bucket/grid.
    available_vehicles::numeric / nullif(total_vehicles, 0) as availability_rate -- Derived trend metric with divide-by-zero protection.
from fact_grid_15min;

-- Summarizes pipeline health over the last 24 hours.
-- This gives the dashboard an operations view of freshness, failures, warnings, latency, and rejection rate.
create or replace view vw_pipeline_health as
select
    max(started_at) as latest_run, -- Most recent attempted pipeline run.
    count(*) filter (
        where started_at at time zone 'America/Los_Angeles'
            >= date_trunc('day', now() at time zone 'America/Los_Angeles')
    )::integer as runs_today, -- Count of runs since Seattle-local midnight for daily monitoring.
    count(*) filter (where quality_status = 'SUCCESS')::numeric / nullif(count(*), 0) as success_rate, -- Share of recent runs that completed cleanly.
    count(*) filter (where quality_status = 'FAILED')::integer as failed_runs, -- Recent failed-run count for alerting.
    count(*) filter (where quality_status = 'WARNING')::integer as warning_runs, -- Recent warning-run count for data-quality attention.
    avg(api_latency_ms)::numeric(12, 2) as average_api_latency_ms, -- Average source API latency to monitor feed responsiveness.
    avg(pipeline_duration_ms)::numeric(12, 2) as average_pipeline_duration_ms, -- Average end-to-end runtime to spot slow ingestion.
    sum(records_rejected)::numeric / nullif(sum(records_received), 0) as rejection_rate, -- Share of records rejected by validation.
    now() - max(completed_at) filter (where quality_status in ('SUCCESS', 'WARNING')) as time_since_last_successful_ingestion -- Freshness indicator for operators.
from pipeline_runs
where started_at >= now() - interval '24 hours'; -- Limits the health view to recent operational behavior.
