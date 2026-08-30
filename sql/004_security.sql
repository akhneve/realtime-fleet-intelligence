-- Restricts Supabase Data API roles from operational tables and views.
-- The ETL and documented Power BI DirectQuery connection use the configured PostgreSQL login, not anon/authenticated API roles.
revoke all privileges on all tables in schema public from anon, authenticated;
alter default privileges in schema public revoke all privileges on tables from anon, authenticated;

-- Defense in depth: later accidental grants still require an explicit row-level policy.
alter table current_vehicle_state enable row level security;
alter table fact_vehicle_snapshot enable row level security;
alter table fact_grid_15min enable row level security;
alter table rejected_records enable row level security;
alter table pipeline_runs enable row level security;
alter table reporting_thresholds enable row level security;
