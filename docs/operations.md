# Operations Runbook

## Migration and role rollout

1. Back up the database and record the current view definitions.
2. For an existing installation, apply every outstanding migration through `009_geography_refresh_status.sql` as the schema owner. New databases apply all numbered files.
3. Confirm every constraint is validated and query each reporting view.
4. Create deployment-specific login roles outside source control, then grant membership:

```sql
create role fleet_ingest_login login password '<secret-from-password-manager>';
grant fleet_ingest to fleet_ingest_login;

create role fleet_powerbi_login login password '<different-secret>';
grant fleet_reporting to fleet_powerbi_login;
```

Grant `CONNECT` and, for ingestion, `TEMP` on the target database to those login roles. Update GitHub and Power BI credentials only after testing the new logins. Do not use the `postgres` owner for routine ingestion or reporting.

## Deployment verification

Before enabling the 15-minute schedule:

```powershell
fleet-diagnostics --live --database
fleet-ingest --dry-run
python scripts/ingest_geography.py --dry-run
python scripts/ingest_geography.py
```

After the first scheduled run, confirm:

- the latest `pipeline_runs` row is `SUCCESS` or an understood `WARNING`;
- `current_vehicle_state` has a plausible count and one run ID;
- `system_information`, `station_information`, and `current_station_status` contain the latest usable run;
- `fact_station_status_snapshot` contains a row for the latest station-status feed;
- `feed_run_metrics` contains four rows for the latest run;
- `fact_grid_15min` contains the latest bucket;
- Power BI reporting logins can read views but cannot insert into `pipeline_runs`.
- all four geography dimensions are nonempty, valid, SRID 4326, and indexed;
- `lookup_vehicle_geography(-122.3321, 47.6062)` returns Seattle geography;
- `lookup_vehicle_geography(-74.0060, 40.7128)` returns four null values.
- `vw_geography_refresh_status.last_successful_update` matches the latest successful monthly load.

## Capacity

At approximately 13,000 vehicles per run, the balanced defaults are expected to consume roughly 0.7–1.0 GB including indexes. Actual size depends on fleet and grid counts. Review this query weekly:

```sql
select
    relname,
    pg_size_pretty(pg_total_relation_size(oid)) as total_size
from pg_class
where relnamespace = 'public'::regnamespace
  and relkind in ('r', 'm')
order by pg_total_relation_size(oid) desc;
```

If storage approaches its limit, reduce vehicle-detail hours first. Keep at least 28–35 aggregate days so each weekday/time bucket can accumulate four or five samples.

## Troubleshooting

- Configuration errors name the offending environment variable and occur before database writes.
- HTTP 401/403/404 responses are permanent configuration/source errors and are not retried.
- HTTP 429, 5xx, connection failures, and timeouts are retried with exponential backoff.
- `WARNING` snapshots update reporting state; `FAILED` snapshots preserve detail/evidence but do not replace any current state.
- A failed database connection cannot be recorded in `pipeline_runs`; use the workflow log.
- If migrations fail during constraint validation, inspect existing invalid rows before retrying. Do not drop the constraint merely to force deployment.

## Rollback

Disabling the scheduled workflow is the first rollback step. The Python deployment can then be reverted without reversing additive database changes because existing view columns were preserved.

If migration 005 itself must be reversed, restore the saved view definitions before dropping its named check constraints. Migration 006 can be rolled back by revoking login memberships; retain the group roles and policies until no active session depends on them. Migrations 007 through 009 are additive, so an application rollback can leave their tables and views in place; remove them only in a separately reviewed, backed-up maintenance change. Never drop roles, geography tables, or constraints during an incident without a backup and an explicit dependency check.
