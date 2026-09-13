# Architecture

## Pipeline boundaries

The package is split around external and transactional boundaries:

- `extract` owns HTTP behavior and reuses one session across the four configured feeds. It retries only connection failures, timeouts, HTTP 429, and 5xx responses; permanent 4xx and malformed JSON fail immediately.
- `validate` has feed-specific contracts for system metadata, station definitions, station status, and GBFS v2 `data.bikes`/v3 `data.vehicles`. It normalizes IDs and aliases and quarantines malformed, duplicate, non-finite, geographically invalid, or logically invalid records.
- `metrics` assesses timestamp freshness, empty snapshots, and volume drops before database state can change.
- `transform` derives availability, time metadata, run lineage, and deterministic grid IDs across all four datasets.
- `load` stages the high-volume vehicle set once with PostgreSQL `COPY`, then performs system/station upserts and all history/current-state writes inside one transaction.

Typed dataclasses are the contracts between stages. Unstructured JSON is confined to the HTTP boundary and rejected-record evidence.

## Transaction invariant

A usable snapshot atomically:

1. writes vehicle and station-status detail;
2. reconciles current system, station, station-status, and vehicle state;
3. replaces the affected 15-minute grid aggregate bucket;
4. stores feed-tagged rejected records, per-feed metrics, and reporting thresholds;
5. applies retention; and
6. records the pipeline run.

If any step fails, PostgreSQL rolls the transaction back. A `FAILED` quality snapshot retains accepted detail and rejection evidence but does not replace current state or aggregates. The process returns nonzero for failed quality or an exception.

The run record is updated once after commit so its final duration includes database work. If that update fails, data remains committed and the run is marked failed when a second database write is still possible; otherwise GitHub Actions logs are the authoritative evidence.

## Reporting semantics

`system_information`, `station_information`, `current_station_status`, and `current_vehicle_state` are authoritative current read models. `fact_vehicle_snapshot` is short-lived forensic detail, `fact_station_status_snapshot` preserves station-level history, and `fact_grid_15min` is compact grid history. `feed_run_metrics` separates endpoint-level health from the backward-compatible vehicle counts in `pipeline_runs`.

Rebalancing compares current availability with the matching Seattle-local weekday and 15-minute bucket from the latest accepted snapshot timestamp. It does not use the dashboard viewer's current clock. Baselines expose their sample count and are not considered ready below four observations.

## Security boundary

Schema-owner credentials apply migrations only. A deployment login inherits `fleet_ingest`; a Power BI login inherits `fleet_reporting`. The former receives required operations on every feed table through RLS policies, while the latter can select reporting views—including station supply and per-feed health—but cannot write operational tables.

No password or login role is created in source control.
