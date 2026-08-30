-- Speeds up current supply lookups by grid.
-- Power BI grid-level maps and tables repeatedly group or filter current vehicles by grid_id.
create index if not exists idx_current_vehicle_state_grid on current_vehicle_state (grid_id);

-- Speeds up latest-ingestion checks on current state.
-- This supports freshness diagnostics and quick inspection of the most recently updated rows.
create index if not exists idx_current_vehicle_state_ingestion on current_vehicle_state (ingestion_timestamp desc);

-- Speeds up recent vehicle-history queries ordered by time.
-- The detailed snapshot table can grow quickly, so time-based retention and debugging need this index.
create index if not exists idx_fact_vehicle_snapshot_timestamp on fact_vehicle_snapshot (snapshot_timestamp desc);

-- Speeds up recent history queries for one geographic grid.
-- This supports grid drill-downs without scanning all vehicle snapshots.
create index if not exists idx_fact_vehicle_snapshot_grid_time on fact_vehicle_snapshot (grid_id, snapshot_timestamp desc);

-- Speeds up trend visuals that filter by grid and sort by time.
-- fact_grid_15min is the long-term reporting table, so this index supports the most common dashboard access pattern.
create index if not exists idx_fact_grid_15min_grid_time on fact_grid_15min (grid_id, bucket_timestamp desc);

-- Speeds up three-day aggregate retention by timestamp without requiring a grid filter.
create index if not exists idx_fact_grid_15min_timestamp on fact_grid_15min (bucket_timestamp desc);

-- Speeds up joining rejected records back to a pipeline run.
-- This is useful when investigating a specific failed or warning ingestion.
create index if not exists idx_rejected_records_run on rejected_records (run_id);

-- Speeds up rejection dashboards grouped by reason over time.
-- Operators need to see whether failures are mostly coordinates, duplicates, malformed records, or schema issues.
create index if not exists idx_rejected_records_reason on rejected_records (reason_code, rejected_at desc);

-- Speeds up latest-run and run-history queries.
-- Pipeline health pages usually sort by newest run first.
create index if not exists idx_pipeline_runs_started on pipeline_runs (started_at desc);

-- Speeds up health summaries filtered by SUCCESS, WARNING, or FAILED.
-- This makes operational status cards and failure counts responsive.
create index if not exists idx_pipeline_runs_status on pipeline_runs (quality_status, started_at desc);
