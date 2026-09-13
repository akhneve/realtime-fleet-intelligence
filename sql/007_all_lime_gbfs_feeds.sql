-- Extends the original free-bike pipeline with the three related Lime GBFS feeds.
alter table rejected_records
    add column if not exists feed_name text not null default 'free_bike_status';

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'rejected_records_feed_name_check'
    ) then
        alter table rejected_records add constraint rejected_records_feed_name_check
            check (feed_name in (
                'system_information', 'station_information',
                'station_status', 'free_bike_status'
            )) not valid;
        alter table rejected_records validate constraint rejected_records_feed_name_check;
    end if;
end
$$;

create table if not exists system_information (
    system_id text primary key,
    name text not null,
    language text not null,
    timezone text not null,
    license_url text,
    attribution_organization_name text,
    source_timestamp timestamptz,
    ingestion_timestamp timestamptz not null,
    run_id uuid not null
);

create table if not exists station_information (
    station_id text primary key,
    name text not null,
    short_name text,
    latitude double precision not null check (latitude between -90 and 90),
    longitude double precision not null check (longitude between -180 and 180),
    region_id text,
    capacity integer check (capacity >= 0),
    source_timestamp timestamptz,
    ingestion_timestamp timestamptz not null,
    run_id uuid not null
);

create table if not exists current_station_status (
    station_id text primary key,
    num_vehicles_available integer not null check (num_vehicles_available >= 0),
    num_docks_available integer not null check (num_docks_available >= 0),
    is_installed boolean not null,
    is_renting boolean not null,
    is_returning boolean not null,
    last_reported timestamptz,
    snapshot_timestamp timestamptz not null,
    source_timestamp timestamptz,
    ingestion_timestamp timestamptz not null,
    run_id uuid not null
);

create table if not exists fact_station_status_snapshot (
    station_id text not null,
    num_vehicles_available integer not null check (num_vehicles_available >= 0),
    num_docks_available integer not null check (num_docks_available >= 0),
    is_installed boolean not null,
    is_renting boolean not null,
    is_returning boolean not null,
    last_reported timestamptz,
    snapshot_timestamp timestamptz not null,
    source_timestamp timestamptz,
    ingestion_timestamp timestamptz not null,
    run_id uuid not null,
    quality_flag text not null check (quality_flag in ('SUCCESS', 'WARNING', 'FAILED')),
    primary key (run_id, station_id)
);

create table if not exists feed_run_metrics (
    run_id uuid not null,
    feed_name text not null check (feed_name in (
        'system_information', 'station_information',
        'station_status', 'free_bike_status'
    )),
    source_url text not null,
    http_status integer not null check (http_status between 100 and 599),
    api_latency_ms integer not null check (api_latency_ms >= 0),
    source_timestamp timestamptz,
    records_received integer not null check (records_received >= 0),
    records_valid integer not null check (records_valid >= 0),
    records_rejected integer not null check (records_rejected >= 0),
    observed_schema_keys jsonb not null default '[]'::jsonb,
    primary key (run_id, feed_name),
    constraint feed_run_metrics_run_fk foreign key (run_id)
        references pipeline_runs (run_id) on delete cascade
        deferrable initially deferred,
    constraint feed_run_metrics_count_check
        check (records_received = records_valid + records_rejected)
);

create index if not exists idx_station_status_snapshot_time
    on fact_station_status_snapshot (snapshot_timestamp desc);
create index if not exists idx_feed_run_metrics_source_time
    on feed_run_metrics (feed_name, source_timestamp desc);

create or replace view vw_current_station_supply as
select
    information.station_id,
    information.name as station_name,
    information.latitude,
    information.longitude,
    information.capacity,
    status.num_vehicles_available,
    status.num_docks_available,
    status.is_installed,
    status.is_renting,
    status.is_returning,
    status.last_reported,
    status.snapshot_timestamp,
    status.source_timestamp
from station_information information
left join current_station_status status using (station_id);

create or replace view vw_feed_health as
select
    metrics.feed_name,
    max(metrics.source_timestamp) as latest_source_timestamp,
    max(runs.completed_at) as latest_ingestion,
    avg(metrics.api_latency_ms)::numeric(12, 2) as average_api_latency_ms,
    sum(metrics.records_rejected)::numeric
        / nullif(sum(metrics.records_received), 0) as rejection_rate
from feed_run_metrics metrics
join pipeline_runs runs using (run_id)
where runs.started_at >= now() - interval '24 hours'
group by metrics.feed_name;

revoke all privileges on
    system_information,
    station_information,
    current_station_status,
    fact_station_status_snapshot,
    feed_run_metrics
from public;

grant select, insert, update, delete on
    system_information,
    station_information,
    current_station_status,
    fact_station_status_snapshot,
    feed_run_metrics
to fleet_ingest;

grant select on vw_current_station_supply, vw_feed_health to fleet_reporting;

alter table system_information enable row level security;
alter table station_information enable row level security;
alter table current_station_status enable row level security;
alter table fact_station_status_snapshot enable row level security;
alter table feed_run_metrics enable row level security;

do $$
declare
    relation_name text;
begin
    foreach relation_name in array array[
        'system_information',
        'station_information',
        'current_station_status',
        'fact_station_status_snapshot',
        'feed_run_metrics'
    ]
    loop
        if not exists (
            select 1 from pg_policies
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
