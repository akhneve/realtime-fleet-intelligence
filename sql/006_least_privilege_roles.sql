-- Group roles carry privileges; deployment-specific login roles receive membership.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'fleet_ingest') then
        create role fleet_ingest nologin nosuperuser nocreatedb nocreaterole noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'fleet_reporting') then
        create role fleet_reporting nologin nosuperuser nocreatedb nocreaterole noinherit;
    end if;
end
$$;

revoke create on schema public from public;
revoke all privileges on all tables in schema public from public;
alter default privileges in schema public revoke all privileges on tables from public;

grant usage on schema public to fleet_ingest, fleet_reporting;
grant select, insert, update, delete on
    current_vehicle_state,
    fact_vehicle_snapshot,
    fact_grid_15min,
    rejected_records,
    pipeline_runs,
    reporting_thresholds
to fleet_ingest;

grant select on
    vw_current_fleet_summary,
    vw_current_grid_supply,
    vw_grid_supply_baseline,
    vw_rebalancing_priority,
    vw_fleet_availability_trend,
    vw_pipeline_health
to fleet_reporting;

alter default privileges in schema public
    grant select, insert, update, delete on tables to fleet_ingest;

-- RLS remains effective for ordinary login roles while the ingest group receives
-- only the row access required by the ETL.
do $$
declare
    relation_name text;
begin
    foreach relation_name in array array[
        'current_vehicle_state',
        'fact_vehicle_snapshot',
        'fact_grid_15min',
        'rejected_records',
        'pipeline_runs',
        'reporting_thresholds'
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
