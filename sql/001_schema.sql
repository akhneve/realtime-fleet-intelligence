-- Enables gen_random_uuid(), which we use for rejected_records.rejection_id.
-- This keeps rejected rows uniquely traceable without the Python ETL needing to create every ID itself.
create extension if not exists pgcrypto;

-- Stores the latest known state for each vehicle.
-- This table is intentionally small and upsert-friendly so Power BI can query current operations quickly.
create table if not exists current_vehicle_state (
    vehicle_id text primary key, -- One row per vehicle; this prevents duplicates in the live operational state.
    vehicle_type_id text, -- Keeps the source vehicle category when available so reports can later split bikes vs scooters.
    latitude double precision not null, -- Current latitude after validation; required because map visuals need a usable point.
    longitude double precision not null, -- Current longitude after validation; required with latitude for geographic reporting.
    is_reserved boolean, -- Source reservation flag; stored for auditability and availability logic transparency.
    is_disabled boolean, -- Source disabled flag; stored so unavailable vehicles can be explained instead of hidden.
    available_flag boolean not null, -- Derived business-ready flag; Power BI should not recalculate core pipeline logic.
    grid_id text not null, -- Rectangular grid bucket used for supply aggregation and rebalancing signals.
    source_timestamp timestamptz, -- Timestamp from the GBFS feed when provided; used to assess feed freshness.
    ingestion_timestamp timestamptz not null, -- Timestamp when our pipeline processed the record; used for observability.
    run_id uuid not null -- Links each current-state row back to the pipeline run that last updated it.
);

-- Stores recent vehicle-level snapshots for short-term history and debugging.
-- This is the hot granular table, kept only for a limited retention window to control database size.
create table if not exists fact_vehicle_snapshot (
    snapshot_timestamp timestamptz not null, -- Business event time for the snapshot, preferring source time when available.
    vehicle_id text not null, -- Vehicle identifier normalized from GBFS bike_id or vehicle_id.
    latitude double precision not null, -- Validated latitude at snapshot time for recent movement and map analysis.
    longitude double precision not null, -- Validated longitude at snapshot time for recent movement and map analysis.
    available_flag boolean not null, -- Availability at snapshot time, preserving history even after current state changes.
    grid_id text not null, -- Grid bucket at snapshot time for comparing vehicle-level history with aggregates.
    run_id uuid not null, -- ETL run identifier; included in the key to make repeated loads idempotent.
    quality_flag text not null, -- Snapshot QA status so reports can filter or inspect warning/failed-quality data.
    primary key (run_id, vehicle_id) -- Prevents duplicate vehicle rows if the same ingestion run is retried.
);

-- Stores compact 15-minute grid aggregates for longer-term reporting.
-- This table is the main Power BI trend source because it avoids keeping months of high-volume vehicle rows.
create table if not exists fact_grid_15min (
    bucket_timestamp timestamptz not null, -- Snapshot time rounded down to a 15-minute reporting bucket.
    grid_id text not null, -- Geographic bucket for supply comparison and rebalancing analysis.
    total_vehicles integer not null, -- Total observed vehicles in the grid during the bucket.
    available_vehicles integer not null, -- Available vehicles in the grid; this drives supply and baseline metrics.
    unavailable_vehicles integer not null, -- Unavailable vehicles retained for transparency and availability-rate math.
    run_id uuid not null, -- Latest run that produced this aggregate, useful when auditing updates.
    primary key (bucket_timestamp, grid_id) -- Allows safe upserts for a bucket/grid if a run is repeated.
);

-- Stores invalid or suspicious records instead of discarding them.
-- This provides data lineage and proves that validation failures are visible to operators.
create table if not exists rejected_records (
    rejection_id uuid primary key default gen_random_uuid(), -- Unique rejection event ID generated inside Postgres.
    run_id uuid not null, -- Connects each rejected row to the ingestion run that saw it.
    record_key text, -- Best available vehicle key; nullable because malformed records may not have one.
    raw_payload jsonb not null, -- Original rejected source JSON for debugging schema or data-quality issues.
    reason_code text not null, -- Machine-readable rejection category for dashboard grouping and alerting.
    reason_detail text not null, -- Human-readable explanation for why the record was rejected.
    rejected_at timestamptz not null default now() -- Timestamp for retention cleanup and rejection trend reporting.
);

-- Stores one observability row per ingestion attempt.
-- Power BI can use this table to show whether the pipeline is fresh, healthy, slow, or failing.
create table if not exists pipeline_runs (
    run_id uuid primary key, -- Unique run identifier created by Python at the start of each ingestion.
    started_at timestamptz not null, -- Start timestamp used to calculate run cadence and daily run counts.
    completed_at timestamptz not null, -- Completion timestamp used for freshness and duration reporting.
    http_status integer, -- GBFS HTTP status when a response exists; null when failure happens before a response.
    records_received integer not null, -- Raw source count before record-level validation.
    records_valid integer not null, -- Accepted record count after validation; used for volume anomaly checks.
    records_rejected integer not null, -- Rejected record count for data-quality monitoring.
    api_latency_ms integer, -- API response latency; helps separate feed slowness from database or transform slowness.
    pipeline_duration_ms integer not null, -- End-to-end runtime for pipeline performance monitoring.
    quality_status text not null check (quality_status in ('SUCCESS', 'WARNING', 'FAILED')), -- Constrained status values keep dashboard filters consistent.
    error_message text, -- Failure or warning details, nullable for clean successful runs.
    observed_schema_keys jsonb not null default '[]'::jsonb -- Captures source keys seen during a run for schema-drift review.
);

-- Stores configurable reporting thresholds used by SQL views.
-- Keeping these values in a table lets the dashboard logic be tuned without editing the view definition.
create table if not exists reporting_thresholds (
    setting_name text primary key, -- Stable name for a threshold used by reporting views.
    setting_value numeric not null, -- Numeric threshold value, such as supply-gap cutoffs.
    updated_at timestamptz not null default now() -- Records when a setting row was created or manually updated.
);

-- Seeds default rebalancing thresholds for the MVP.
-- The on-conflict clause preserves any manually tuned values if this migration is rerun.
insert into reporting_thresholds (setting_name, setting_value)
values
    ('rebalance_high_threshold', 10), -- HIGH priority starts when current supply is at least 10 below baseline.
    ('rebalance_medium_threshold', 5) -- MEDIUM priority starts when current supply is at least 5 below baseline.
on conflict (setting_name) do nothing;
