# Real-Time Fleet Operations Intelligence

This repository is a production-oriented data pipeline for observing Lime's Seattle micromobility fleet. Every 15 minutes it downloads all four configured GBFS feeds (`system_information`, `station_information`, `station_status`, and `free_bike_status`), validates and transforms them into stable internal models, writes PostgreSQL atomically, and exposes Power BI-compatible reporting views.

The system is near-real-time availability reporting. It observes system metadata, station supply, operating flags, and free vehicles published at discrete moments; it does not receive trip events, prove demand, or continuously track movement.

## The Problem, From First Principles

The source APIs describe the Lime system, its stations, station-level supply, and each currently reported free vehicle. A production dashboard needs more than those raw JSON responses.

Before the feed can support operational decisions, the system must solve several separate problems:

1. **Networks are unreliable.** Requests can time out, receive rate limits, or fail with temporary server errors.
2. **Source data is untrusted input.** A response can be malformed, stale, unexpectedly empty, duplicated, or contain impossible coordinates.
3. **Source schemas evolve.** GBFS v2 calls the collection `bikes`; GBFS v3 calls it `vehicles`. Downstream reports should not know or care which source alias was used.
4. **Related feeds have different grains.** Singleton system metadata, station definitions, station status, and free vehicles cannot be forced into one record shape without losing meaning.
5. **Current state and history have different jobs.** Operators need the latest vehicle and station state quickly, while analysts need historical trends without scanning millions of raw rows.
6. **Partial writes are misleading.** A dashboard must never show one feed from a new run while related state, aggregates, or quality metadata still represent an older run.
7. **Bad data must remain explainable.** Rejecting a record silently makes failures impossible to investigate, and a rejection must identify which feed produced it.
8. **Storage is finite.** Vehicle detail grows much faster than station or grid history, so each data product needs an appropriate retention period.
9. **Production access must be limited.** The ingestion job needs write access; Power BI needs read access; neither needs schema-owner privileges.
10. **Failures are part of the product.** Operators need pipeline-level and endpoint-level evidence of failed, warning, and successful runs—not merely the successful data.

The repository separates these concerns so each rule has one owner and can be tested independently.

## The System in One Picture

```text
Four Lime GBFS HTTPS feeds
             |
             v
      extract with bounded retries
             |
             v
   validate + normalize + quarantine
             |
             v
 snapshot-level quality assessment
             |
             v
 transform to typed system, station, and vehicle records
             |
             | one PostgreSQL transaction
             v
 system/station state + vehicle state + histories
       + rejected records + per-feed run evidence
             |
             v
 stable SQL reporting views -> Power BI
```

GitHub Actions provides the clock and runs the ingestion command every 15 minutes. PostgreSQL is the system of record. Power BI is a presentation consumer: it does not clean source data or redefine business rules.

## What Happens During One Run

The production entry point is `fleet-ingest`. One invocation performs the following sequence:

1. Load and validate pipeline configuration.
2. Load database configuration only because this is a production run; dry runs do not require database credentials.
3. Open a TLS-required PostgreSQL connection and read the recent valid-record baseline.
4. Fetch all four GBFS documents through one reusable HTTP session with separate connection/read timeouts and bounded exponential backoff.
5. Validate system metadata, station definitions, station statuses, and either GBFS v2 `data.bikes` or GBFS v3 `data.vehicles`.
6. Normalize IDs and aliases, reject malformed station/vehicle records, and reject every copy of duplicate normalized IDs.
7. Assess timestamp freshness and zero-record protection across all feeds, plus vehicle volume-drop quality.
8. Derive availability, deterministic geographic grid IDs, timestamps, and run lineage for every dataset.
9. Copy accepted vehicles into a temporary PostgreSQL staging table once; the much smaller system and station sets use parameterized upserts.
10. Use set-based SQL to write current state and history for all four feeds, replace the affected grid bucket, store feed-tagged rejections and per-feed metrics, apply retention, and write the run record.
11. Commit all reporting changes together. If any database operation fails, PostgreSQL rolls the transaction back.
12. Update the run's final duration so observability includes database work.

A `SUCCESS` or `WARNING` snapshot updates operational state. A `FAILED` quality snapshot remains auditable but cannot replace current state or long-term aggregates. Exceptions and failed-quality runs return a nonzero process status so the scheduler marks the run as failed.

## Four-Feed Source Contract

Each endpoint keeps its native grain through validation and transformation. The loader combines them only at the transaction boundary:

| Feed | Expected source shape | Normalized content | Database destination |
|---|---|---|---|
| `system_information` | Singleton object in `data` | Required system ID, name, language, and timezone; optional license URL and attribution organization. | `system_information`, replaced as the authoritative current system record. |
| `station_information` | `data.stations[]` | Station ID, name, optional short name/region/capacity, and finite coordinates. Duplicate IDs, invalid coordinates, and optional bounding-box violations are quarantined. | `station_information`, reconciled as the authoritative current station dimension. |
| `station_status` | `data.stations[]` | Station ID, non-negative vehicle/dock counts, installed/renting/returning flags, and optional `last_reported`. Both `num_vehicles_available` and the older `num_bikes_available` alias are accepted. | `current_station_status` for operations and `fact_station_status_snapshot` for history. |
| `free_bike_status` | GBFS v2 `data.bikes[]` or v3 `data.vehicles[]` | Vehicle ID, type, finite coordinates, reservation state, disabled state, derived availability, and deterministic grid ID. | `current_vehicle_state`, `fact_vehicle_snapshot`, and the derived `fact_grid_15min` aggregate. |

All endpoint URLs must be absolute HTTPS URLs. Extraction reuses one HTTP session but records latency, HTTP status, source timestamp, source URL, counts, and observed keys independently for each feed in `feed_run_metrics`. A failure identifies the feed by name and aborts the four-feed refresh.

## Seattle Geography Enrichment

Static public boundaries are loaded once initially and refreshed when the source data changes. Live vehicle coordinates are enriched in PostgreSQL, not in Power BI, through `lookup_vehicle_geography(longitude, latitude)` and `vw_vehicle_geography_enriched`.

| Source | Properties observed | Source features | Loaded dimension rows |
|---|---|---:|---:|
| [Neighborhoods](https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/master/data/neighborhoods.geojson) | `area`, `city`, `county`, `name`, `nested`, `nhood` | 175 | 90 Seattle rows in `dim_neighborhood` |
| [ZIP codes](https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/master/data/zip-codes.geojson) | `AFFGEOID10`, `ALAND10`, `AWATER10`, `GEOID10`, `ZCTA5CE10` | 32 | 32 rows in `dim_zip_area` |
| [City council districts](https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/master/data/city-council-districts.geojson) | `district` | 7 | 7 rows in `dim_council_district` |

The neighborhood file is King County-wide despite its filename. The loader validates all 175 geometries, explicitly logs 85 non-Seattle scope exclusions, and loads the 90 records whose `city` property is `Seattle`. The source currently contains 57 topologically invalid neighborhood polygons. Each is logged with its validity reason, repaired with Shapely, normalized to `MultiPolygon`, and revalidated; an unrecoverable feature fails and rolls back the entire load rather than being silently dropped.

Migration 008 enables PostGIS, stores every polygon as `geometry(MultiPolygon, 4326)`, adds GiST indexes, and derives `dim_grid` from 0.01-degree cells clipped to the union of council districts. The grid formula is the same one used by `assign_grid_id`. Keep `GEOGRAPHY_GRID_SIZE_DEGREES` equal to `GRID_SIZE_DEGREES` if either setting is changed.

The council union is the city mask. A vehicle must match that mask before any neighborhood, ZIP, or grid is returned, so records outside Seattle produce four null geography values. `ST_Covers` includes points exactly on polygon boundaries. Lateral lookups select at most one row per dimension, preventing overlapping source polygons from duplicating vehicle facts.

The `refresh-seattle-geography` GitHub Actions workflow runs on the first day of every month at 09:17 UTC and can also be started with `workflow_dispatch`. Run the geography loader separately from the 15-minute vehicle pipeline because these boundaries change infrequently:

```powershell
# Validates HTTP responses, feature properties, topology, and expected counts; no DB writes.
python scripts/ingest_geography.py --dry-run

# Idempotent atomic upsert, stale-row reconciliation, grid refresh, and database verification.
python scripts/ingest_geography.py
```

The database run verifies nonempty dimensions, valid geometry, SRID 4326, all four spatial indexes, a downtown Seattle lookup, and a far-outside lookup. The checked deployment output is recorded in [docs/geography_verification.md](docs/geography_verification.md).

Every successful upsert stores a UTC `loaded_at` value on each dimension row. `vw_geography_refresh_status` summarizes this as `last_successful_update`, row count, and elapsed time for each table. Because all writes are atomic, a failed refresh rolls back and leaves the prior successful update date intact.

Ready-to-run SQL is in [sql/examples/verify_geography.sql](sql/examples/verify_geography.sql) and [sql/examples/power_bi_geography.sql](sql/examples/power_bi_geography.sql). Files under `sql/examples/` are operator queries, not migrations.

## Why the Database Has Several Tables

One table cannot efficiently represent every grain of information:

| Object | Grain | Why it exists | Place in the system |
|---|---|---|---|
| `current_vehicle_state` | One row per currently reported vehicle | Makes current maps and counts fast without searching history. | Operational read model replaced by each usable full snapshot. |
| `fact_vehicle_snapshot` | One vehicle per ingestion run | Preserves short-lived forensic detail for debugging recent changes. | Detailed history retained for 24 hours by default. |
| `fact_grid_15min` | One grid per 15-minute bucket | Stores compact trends and makes multi-week baselines affordable. | Analytical history retained for 35 days by default. |
| `system_information` | One currently reported system | Stores operator identity, language, timezone, licensing, and attribution metadata. | Current reference data refreshed by each usable run. |
| `station_information` | One currently reported station | Stores names, coordinates, capacity, and other station reference fields. | Authoritative current station dimension. |
| `current_station_status` | One latest status per station | Makes station supply and operating flags fast to query. | Operational station read model. |
| `fact_station_status_snapshot` | One station status per ingestion run | Preserves station-level supply, flags, source/report times, lineage, and quality. | Historical station fact retained for `AGGREGATE_RETENTION_DAYS`, 35 days by default. |
| `feed_run_metrics` | One feed per ingestion run | Preserves each endpoint's URL, latency, HTTP status, source timestamp, received/valid/rejected counts, and observed schema. | Per-feed observability linked to `pipeline_runs` and removed with its retained parent run. |
| `rejected_records` | One rejected station or vehicle record | Keeps the original bad input, source feed name, record key, and machine/human-readable reasons. | Feed-aware data-quality quarantine retained for 14 days by default. |
| `pipeline_runs` | One four-feed ingestion attempt | Records end-to-end timing, vehicle-scoped counts/schema, combined endpoint latency, overall quality, and errors. | Backward-compatible pipeline evidence retained for 90 days by default. |
| `reporting_thresholds` | One named threshold | Lets reporting logic use controlled operational settings. | Configuration bridge between environment settings and SQL views. |

The same run is intentionally represented at different grains. Current-state tables optimize “what is true now?”, detail facts optimize investigation, grid aggregates optimize longer trends, and the run tables separate whole-pipeline health from individual endpoint health.

`pipeline_runs.records_received`, `records_valid`, `records_rejected`, and `observed_schema_keys` deliberately remain vehicle-scoped so the original count invariant and historical dashboards remain compatible. `pipeline_runs.api_latency_ms` is the sum of the four endpoint latencies. Use `feed_run_metrics` whenever an operator needs feed-specific counts, schemas, timestamps, or latency.

## Repository Map: Why Every File Exists

### Root files

| File | What it does | Why it is needed | Where it fits |
|---|---|---|---|
| `README.md` | Explains the problem, architecture, setup, repository contents, and supported behavior. | Code alone cannot communicate system intent or operational boundaries to a new engineer. | Primary human entry point. |
| `pyproject.toml` | Defines the version `1.1.0` multi-feed package, Python compatibility, console commands, direct dependencies, package discovery, and Ruff/Mypy/pytest/coverage configuration. | Python tooling needs one machine-readable project contract instead of unrelated command-line conventions. | Build, installation, and quality-tool control plane. |
| `requirements.txt` | Pins direct runtime dependencies used by the application. | Makes the small runtime dependency surface explicit and easy to audit. | Human-readable runtime dependency manifest synchronized with `pyproject.toml`. |
| `requirements.lock` | Pins the fully resolved runtime environment, including transitive dependencies. | A scheduled job should install the same versions on every runner rather than resolving a different environment over time. | Reproducible production and scheduled-workflow installation. |
| `requirements-dev.txt` | Adds Mypy, pytest, coverage, and Ruff to the runtime requirements. | Contributors need development tools that production does not need. | Human-readable local development dependency manifest. |
| `requirements-dev.lock` | Pins the complete resolved development and CI environment. | CI and local verification must use predictable tool versions. | Reproducible quality-gate installation. |
| `.env.example` | Lists the four Lime endpoints plus database, retention, quality, bounding-box, operational-grid, geography-grid, and reporting settings with safe defaults. | It documents the expanded configuration interface without exposing credentials. | Template for local `.env` and GitHub secrets. |
| `.gitignore` | Excludes secrets, virtual environments, bytecode, build output, coverage data, test output, tool caches, and the local Power BI workspace. | Machine-specific, binary, and generated files create noise, cannot be reviewed usefully as text diffs, and can leak connection metadata. | Repository hygiene and secret-protection boundary. |

### Local-only artifacts

These paths are part of a developer's working system but are intentionally ignored by Git:

| Path | What it does | Why it is local-only | Where it fits |
|---|---|---|---|
| `.env` | Holds real local endpoint and database settings. | It contains credentials and deployment-specific values that must never be committed. | Local configuration source loaded by `config.py`. |
| `.venv/` | Contains the installed Python interpreter environment and dependencies. | It is large, platform-specific, and reproducible from the lock files. | Local execution environment, not source code. |
| `PBI/Fleet Intellegence Dashboard.pbix` | Contains the Power BI Desktop dashboard that presents fleet and pipeline views. | PBIX is a binary, environment-bound authoring artifact that is not meaningfully mergeable and may retain connection metadata. | Presentation layer consuming the stable SQL reporting contract. |
| `__pycache__/`, `*.pyc`, and tool-cache folders | Store generated bytecode and analysis/test caches. | Python and the quality tools recreate them automatically; retaining them would only record machine state. | Local performance artifacts with no architectural responsibility. |

### GitHub automation

| File | What it does | Why it is needed | Where it fits |
|---|---|---|---|
| `.github/workflows/ci.yml` | Runs formatting, linting, strict typing, coverage-enforced tests, and PostgreSQL integration tests on Python 3.12 and 3.13. | A change should prove deterministic correctness before reaching the scheduled production workflow. | Pull-request and main-branch quality gate. |
| `.github/workflows/ingest.yml` | Installs runtime-only dependencies, supplies all four endpoint settings and the legacy free-bike override, and executes `fleet-ingest` every 15 minutes or on manual dispatch. | The pipeline needs an external clock, deployment configuration, and visible execution history. | Production scheduler and runtime environment. |
| `.github/workflows/geography.yml` | Runs the static geography loader on the first day of every month and supports manual dispatch. | Boundary classifications need a regular freshness check without joining the 15-minute feed workload. | Monthly geography refresh and verification schedule. |
| `.github/dependabot.yml` | Checks Python and GitHub Actions dependencies weekly and groups related updates. | Exact pins become unsafe if nobody reviews newer security and compatibility releases. | Dependency maintenance automation. |

CI and ingestion are intentionally separate. Tests should be deterministic and run on code changes; live ingestion should be small, fast, and concerned only with one atomic four-feed production snapshot.

### Design and operations documentation

| File | What it does | Why it is needed | Where it fits |
|---|---|---|---|
| `docs/architecture.md` | Describes module boundaries, typed contracts, transaction invariants, reporting semantics, and security boundaries. | Maintainers need the reasoning behind boundaries, not just setup commands. | Engineering design reference. |
| `docs/operations.md` | Documents migrations, role provisioning, deployment checks, sizing, troubleshooting, and rollback. | Operating a data pipeline safely requires procedures that do not belong inside application code. | Production runbook. |
| `docs/geography_verification.md` | Records the checked source schemas, repair counts, loaded row counts, indexes, SRIDs, and sample lookup results. | The deployed spatial layer needs reproducible evidence beyond a successful process exit. | Geography deployment evidence. |

### Static geography ingestion

| File | What it does | Why it is needed | Where it fits |
|---|---|---|---|
| `scripts/ingest_geography.py` | Downloads all three boundary files with retries, validates every feature, repairs and normalizes polygons, atomically upserts the dimensions, reconciles stale rows, refreshes the clipped grid, and runs database checks. | Slow-changing boundaries need an idempotent owner separate from the high-frequency GBFS pipeline. | On-demand or scheduled static reference-data load. |

### Python package

All runtime code lives under `src/fleet_intelligence`. The `src` layout keeps the import package distinct from the repository root and matches the structure installed into production.

| File | What it does | Why it is needed | Where it fits |
|---|---|---|---|
| `src/fleet_intelligence/__init__.py` | Declares the package and its `1.1.0` version. | Python needs a stable import namespace and a release marker for the multi-feed contract. | Package identity. |
| `src/fleet_intelligence/__main__.py` | Routes `python -m fleet_intelligence` to the ingestion CLI. | Provides a standard Python module entry point in addition to the installed console command. | Thin command bootstrap; contains no business logic. |
| `src/fleet_intelligence/models.py` | Defines JSON/feed types and dataclasses for normalized system, station, status, vehicle, rejection, snapshot, aggregate, validation, and run records. | Stages need explicit contracts so missing or misnamed fields are caught by Mypy rather than during a production load. | Shared internal data language used by every pipeline stage. |
| `src/fleet_intelligence/config.py` | Loads `.env`; validates four HTTPS endpoints, ranges, thresholds, bounding boxes, SSL, and timeouts; exposes a typed feed-name-to-URL map; and keeps `GBFS_URL` as the legacy fallback for `FREE_BIKE_STATUS_URL`. | Invalid configuration must fail before network or database mutation while existing free-bike deployments retain a controlled upgrade path. | Startup boundary between deployment configuration and typed application settings. |
| `src/fleet_intelligence/extract.py` | Reuses one HTTP session across four feeds, sets the versioned user agent, applies timeouts, retries transient failures, parses JSON, measures each endpoint's latency, and names the failed feed in errors. | External networks fail differently from bad data; only recoverable failures should be retried, and operators must know which endpoint stopped the run. | Source boundary where untrusted remote JSON enters the system. |
| `src/fleet_intelligence/validate.py` | Applies feed-specific envelopes and types; normalizes system/station/vehicle IDs and aliases; detects duplicate station/vehicle IDs; rejects invalid coordinates, capacities, counts, state, and report time; applies the optional bounding box to stations and vehicles; and tags quarantined rows with their feed. | Downstream code should receive trusted feed-specific models regardless of source-version details. | Data-quality gate and feed-aware quarantine producer. |
| `src/fleet_intelligence/metrics.py` | Evaluates missing/stale/future timestamps and empty results for every feed, applies volume-drop detection to vehicles, and creates 15-minute grid aggregates. | Valid individual records can still form a suspicious snapshot, and reporting needs compact history. | Snapshot-level quality and analytical aggregation layer. |
| `src/fleet_intelligence/transform.py` | Converts normalized system, station, status, and vehicle values into immutable database records, derives availability, and assigns deterministic grid IDs. | Business-ready fields should be calculated once instead of reimplemented in SQL or Power BI. | Pure transformation layer between validation and persistence. |
| `src/fleet_intelligence/load.py` | Opens hardened PostgreSQL connections, stages vehicles with `COPY`, idempotently reconciles system/station/vehicle current state, appends vehicle and station-status facts, writes aggregates/feed metrics/feed-tagged rejections/run evidence, and applies cascading retention in one transaction. | Database mutations must be atomic, parameterized, efficient, and isolated from source parsing. | Persistence boundary and transaction owner. |
| `src/fleet_intelligence/main.py` | Fetches and validates all four feeds, merges their freshness and completeness into one severity, transforms every dataset, preserves vehicle-scoped legacy metrics, creates four `FeedRunMetric` rows, coordinates production/dry-run flows, sanitizes errors, and implements exit semantics. | A scheduler needs one command that owns sequencing and produces an unambiguous success/failure status without hiding which feed caused a warning. | Application orchestration and `fleet-ingest` implementation. |
| `src/fleet_intelligence/diagnostics.py` | Performs explicit read-only validation of all four live feeds and checks every required table and new reporting view in the configured database. | Deterministic unit tests cannot prove that today's endpoints, credentials, TLS path, or migration 007 relations are reachable. | Operator-facing `fleet-diagnostics` integration check. |
| `src/fleet_intelligence/py.typed` | Marks the installed package as providing inline type information. | External type checkers otherwise treat the installed package as untyped even though its code is annotated. | Packaging metadata for PEP 561-compatible type checking. |

The module order mirrors the data flow: `config -> extract -> validate -> metrics -> transform -> load`, with `models` shared across stages and `main` coordinating them.

### SQL migrations

Migrations are applied in numerical order. They are deliberately separate from runtime startup because schema changes require owner privileges and an explicit operational decision.

| File | What it does | Why it is needed | Where it fits |
|---|---|---|---|
| `sql/001_schema.sql` | Creates extensions, six operational tables, primary keys, basic status constraints, and default reporting thresholds. | Persistence needs a durable physical model before the application can write data. | Initial database foundation. |
| `sql/002_indexes.sql` | Adds indexes for grid lookups, time filtering, retention deletion, rejection investigation, and pipeline status queries. | Correct SQL can still be operationally unusable if common queries require full-table scans. | Physical database performance layer. |
| `sql/003_views.sql` | Defines current fleet, current grid, historical baseline, rebalancing, trend, and pipeline-health views. | Power BI needs stable business-facing datasets without duplicating core SQL in each report. | Initial semantic/reporting layer. |
| `sql/004_security.sql` | Revokes Supabase `anon`/`authenticated` access and enables row-level security on operational tables. | Public API roles must not inherit access to raw operational data accidentally. | Original database security boundary. |
| `sql/005_production_hardening.sql` | Adds validated coordinate/count/quality constraints and updates baseline/rebalancing views to expose sample readiness and use the latest snapshot's time context. | Application validation is not enough by itself; the database must defend its invariants, and stale snapshots must not be compared with the viewer's wall clock. | Additive integrity and reporting-correctness migration for existing installations. |
| `sql/006_least_privilege_roles.sql` | Creates non-login ingestion/reporting roles, hardens public/default privileges, grants minimal access, and installs ingestion RLS policies. | Runtime writers and reporting readers require different privileges, while credentials remain deployment-specific. | Additive least-privilege authorization model. |
| `sql/007_all_lime_gbfs_feeds.sql` | Adds a feed name to quarantined records; creates five system/station/feed-metric tables, station/feed indexes, `vw_current_station_supply`, and `vw_feed_health`; and extends constraints, grants, RLS, and ingest policies. | Existing free-bike deployments need an additive, idempotent path to ingest the other three Lime datasets without rebuilding established vehicle objects. | Four-feed storage, observability, reporting, and security extension. |
| `sql/008_geography_enrichment.sql` | Enables PostGIS; creates neighborhood, ZIP, council, and clipped-grid dimensions; adds GiST indexes; and defines refresh, lookup, and enriched-vehicle interfaces with least-privilege access. | Coordinates need stable server-side spatial attributes so reports do not guess geography or multiply vehicle rows. | Static geography dimension and Power BI semantic layer. |
| `sql/009_geography_refresh_status.sql` | Exposes the stored per-row load timestamps as a per-dimension last-success status view. | Operators and Power BI need a durable freshness date that does not advance when a load fails. | Geography refresh monitoring layer. |

The first four migrations remain the compatibility baseline. Existing databases apply 005 through 009 rather than rebuilding tables or changing established Power BI columns.

### Automated tests

| File | What it proves | Why it is needed | Where it fits |
|---|---|---|---|
| `tests/test_config.py` | Four endpoint defaults and ordering, legacy `GBFS_URL` compatibility, dotenv precedence, HTTPS/SSL errors, retention, thresholds, bounding boxes, required database values, and password redaction. | Configuration failures occur before all other work and the compatibility fallback must remain deterministic. | Startup/configuration regression coverage. |
| `tests/test_extract.py` | Headers, four-feed session reuse/closure, successful extraction, retries, permanent errors, malformed JSON, ownership, and feed-named multi-feed errors. | HTTP retry mistakes can either drop recoverable runs or repeatedly hammer a permanently invalid endpoint. | Source-boundary unit coverage. |
| `tests/test_validate.py` | System identity, station definition/status, vehicle validation, GBFS aliases, duplicates, malformed records, invalid counts/state/coordinates/timestamps, bounding boxes, and feed-tagged quarantine. | Validation is the primary barrier between four untrusted JSON shapes and operational state. | Data-quality regression coverage. |
| `tests/test_transform.py` | System/station/status/vehicle transformations, lineage, station snapshot fallback, availability, grid determinism, aggregation, freshness, and volume-drop behavior. | Pure business rules should remain deterministic across feed expansion and refactors. | Transformation and metrics unit coverage. |
| `tests/test_load.py` | TLS/timeouts, vehicle `COPY` staging, idempotent system/station/status/feed-metric SQL, feed-tagged rejections, retention, failed-snapshot isolation, and transactions. | Persistence bugs can silently produce plausible but inconsistent cross-feed dashboards. | Database-boundary unit coverage with recording fakes. |
| `tests/test_main.py` | Four-feed orchestration and metric creation, related-feed severity escalation, exit statuses, retention propagation, final duration, secret sanitization, connection cleanup, dry-run isolation, and CLI switching. | Orchestration must remain correct when any individual feed or stage fails. | Application-flow regression coverage. |
| `tests/test_diagnostics.py` | Read-only database behavior, four-feed live validation, empty vehicle failure, CLI target selection, and diagnostic exit status. | Operator tools must not mutate production and must fail clearly when any required dependency is unusable. | Diagnostic-command unit coverage. |
| `tests/test_sql.py` | Migration ordering through 009, additive constraints, station/feed/geography tables and views, spatial indexes, refresh timestamps, snapshot-time context, baseline readiness, least-privilege roles, and RLS policies. | Important behavior lives in SQL and needs reviewable regression guards even without a database process. | Static migration contract coverage. |
| `tests/test_automation.py` | All four workflow endpoint variables, the 15-minute schedule, workflow permissions/timeouts, CI Python matrix, PostgreSQL service, coverage gate, and immutable action pins. | Automation configuration is executable production behavior, not incidental YAML. | CI/deployment regression coverage. |
| `tests/test_postgres_integration.py` | Applies every migration twice, transactionally loads all four normalized datasets and metrics, queries vehicle/station views, verifies constraint rollback, and checks ingestion/reporting privileges. | Fakes cannot prove that PostgreSQL accepts the cross-feed SQL, deferred feed-metric relationship, or role semantics. | Real PostgreSQL integration test, enabled by `TEST_DATABASE_URL`. |

Unit tests are deterministic and do not contact the live feed or configured production database. External checks are explicit through `fleet-diagnostics`; PostgreSQL integration uses an isolated CI service.

## Setup

Python 3.12 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
Copy-Item .env.example .env
```

Fill the required PostgreSQL values in `.env`. For a new database, apply `sql/001_schema.sql` through `sql/009_geography_refresh_status.sql` in order. For an existing installation, take a backup and apply every numbered migration it has not yet received. Migration 007 is required before deploying this four-feed application version, migration 008 is required before running the geography loader, and migration 009 exposes its last-success timestamps.

The application never applies migrations automatically. Runtime credentials should not be able to change the schema.

## Commands

```powershell
# Deterministic local quality gate
python -m ruff format --check src tests scripts
python -m ruff check src tests scripts
python -m mypy
python -m pytest --cov=fleet_intelligence --cov-fail-under=90

# Fetch, validate, assess, and transform without database access
fleet-ingest --dry-run

# Explicit read-only dependency checks
fleet-diagnostics --live
fleet-diagnostics --database
fleet-diagnostics --live --database

# One production ingestion
fleet-ingest

# Equivalent module entry point
python -m fleet_intelligence
```

`fleet-ingest --dry-run` is the safest four-feed preflight: it requires network access but no database credentials, validates and quality-checks every live document, transforms vehicle samples plus system/station samples, and logs counts without loading PostgreSQL. `fleet-diagnostics --live` validates that all four endpoints contain usable records. `fleet-diagnostics --database` is read-only and confirms that the original relations plus the five migration-007 tables and two new views exist. Plain `fleet-ingest` is the only command above that performs the production load.

A healthy live preflight includes `status=SUCCESS`, `system=lime_seattle`, nonzero station/status/vehicle counts, and normally zero rejections. Counts are live source observations and therefore vary between runs.

## Configuration

Shell and CI variables override `.env`. Blank optional values use defaults.

| Variable | Default | Purpose |
|---|---:|---|
| `SYSTEM_INFORMATION_URL` | `.../system_information.json` | System metadata source URL. |
| `STATION_INFORMATION_URL` | `.../station_information.json` | Station definition source URL. |
| `STATION_STATUS_URL` | `.../station_status.json` | Station supply/status source URL. |
| `FREE_BIKE_STATUS_URL` | `.../free_bike_status.json` | Preferred free-vehicle source URL. |
| `GBFS_URL` | Disabled legacy override | Backward-compatible free-vehicle URL used only when `FREE_BIKE_STATUS_URL` is blank or absent. |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Required | Dedicated ingestion login. |
| `DB_PORT` | `5432` | PostgreSQL port. |
| `DB_SSLMODE` | `require` | Allowed values: `require`, `verify-ca`, `verify-full`. |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | Initial connection timeout. |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `120` | Per-statement database timeout. |
| `GEOGRAPHY_GRID_SIZE_DEGREES` | `0.01` | Grid-cell size used by `refresh_dim_grid`; keep equal to `GRID_SIZE_DEGREES`. |
| `DETAIL_RETENTION_HOURS` | `24` | Vehicle-level forensic history. |
| `AGGREGATE_RETENTION_DAYS` | `35` | Grid history used by weekday/time baselines and station-status snapshot history. |
| `PIPELINE_RUN_RETENTION_DAYS` | `90` | Pipeline evidence and its cascading per-feed metric rows. |
| `REJECTED_RETENTION_DAYS` | `14` | Quarantined source payloads. |
| `FEED_STALE_MINUTES` | `10` | Source-age warning threshold applied independently to every feed. |
| `VOLUME_DROP_THRESHOLD` | `0.50` | Fractional drop below the recent baseline. |
| `VOLUME_DROP_POLICY` | `WARNING` | Whether a drop produces `WARNING` or `FAILED`. |
| `GRID_SIZE_DEGREES` | `0.01` | Rectangular aggregation cell size. |
| `REBALANCE_HIGH_THRESHOLD` | `10` | High-priority supply gap. |
| `REBALANCE_MEDIUM_THRESHOLD` | `5` | Medium-priority supply gap. |
| `SEATTLE_BBOX_MIN_LAT` and companions | Disabled | Optional complete four-coordinate acceptance boundary for vehicles and station definitions. |

The four explicit endpoint variables are the preferred interface. For compatibility, free-bike URL precedence is `FREE_BIKE_STATUS_URL`, then legacy `GBFS_URL`, then the built-in Lime Seattle URL. Blank optional values from `.env` or GitHub secrets behave as absent and therefore use defaults.

## Upgrade and Deployment Contract

Version `1.1.0` is the first four-feed release. Application code and schema migration 007 are a coordinated deployment: the new loader references migration-007 tables during every production run, including retention cleanup, so deploying the Python package before applying the migration will fail safely and leave the transaction rolled back.

| Deployment case | Required action | Why |
|---|---|---|
| New PostgreSQL/Supabase database | Apply migrations 001 through 009 in numeric order, then configure the ingestion login. | Python ingestion intentionally never creates or alters its own schema. |
| Existing database already on 006 | Back up the database and apply migrations 007, 008, and 009 in order before updating the application. | All three migrations are additive and preserve established vehicle table columns. |
| Existing deployment using `GBFS_URL` | Keep the variable temporarily or migrate it to `FREE_BIKE_STATUS_URL`. | The compatibility fallback prevents an immediate configuration break while explicit feed names become the preferred interface. |
| GitHub Actions deployment | Add the four endpoint secrets only when overriding the built-in Lime URLs; keep database credentials in repository secrets. | Blank endpoint secrets fall back to defaults, while database credentials remain required. |

Migration 007 is rerunnable: it uses conditional table/index creation, replaceable views, and conditional policy creation. It also applies count/coordinate/quality constraints, revokes public access, grants the new objects to `fleet_ingest`/`fleet_reporting`, enables RLS, and creates ingest policies consistent with migration 006.

Migration 008 is also rerunnable. Apply it as the schema owner, then run `scripts/ingest_geography.py` with an ingestion login. PostGIS extension creation is intentionally a migration responsibility rather than a runtime privilege.

Migration 009 is rerunnable and adds only the reporting status view. The monthly workflow assumes migrations 008 and 009 are already deployed; it never grants itself schema-changing privileges.

## Reporting Contract

Power BI reads stable views rather than operational tables:

| View | Intended use |
|---|---|
| `vw_current_fleet_summary` | Current fleet totals, availability, source freshness, and latest pipeline status. |
| `vw_current_grid_supply` | Current vehicles and availability rate per geographic grid. |
| `vw_grid_supply_baseline` | Typical supply for each grid, Seattle-local weekday, and 15-minute time slot. |
| `vw_rebalancing_priority` | Current-versus-historical supply gap and `HIGH`/`MEDIUM`/`LOW` operational signal. |
| `vw_fleet_availability_trend` | Compact historical availability trends. |
| `vw_pipeline_health` | Recent run success, warnings, failures, latency, duration, rejection rate, and freshness. |
| `vw_current_station_supply` | Current station metadata, vehicle/dock counts, and operating flags. |
| `vw_feed_health` | Per-feed freshness, ingestion time, latency, and rejection rate over 24 hours. |
| `vw_vehicle_geography_enriched` | Vehicle snapshots enriched with one neighborhood, ZIP, council district, and Seattle-clipped grid ID. |
| `vw_geography_refresh_status` | Row counts and stored last-successful-update timestamps for each geography dimension. |

Migration 005 appends `baseline_sample_count` and `baseline_ready` without renaming existing columns. Priority remains `LOW` until a grid/time context has at least four historical samples.

`vw_current_station_supply` left-joins station definitions to current status so configured stations remain visible even if status is temporarily absent. `vw_feed_health` groups the last 24 hours by feed and exposes latest source/ingestion time, average endpoint latency, and rejection rate. These additions do not rename or remove any original reporting columns.

### Power BI geography queries

Use the enriched view as the vehicle-detail source. Its four text geography columns can be used directly as slicers:

```sql
select
    vehicle_id,
    snapshot_timestamp,
    latitude,
    longitude,
    available_flag,
    neighborhood_name,
    zip_code,
    council_district_name,
    grid_id,
    source_timestamp,
    ingestion_timestamp
from vw_vehicle_geography_enriched;
```

A compact dataset for availability visuals and slicers is:

```sql
select
    snapshot_timestamp,
    neighborhood_name,
    zip_code,
    council_district_name,
    grid_id,
    count(*) as total_vehicles,
    count(*) filter (where available_flag) as available_vehicles
from vw_vehicle_geography_enriched
group by 1, 2, 3, 4, 5;
```

Test a downtown point and an out-of-city point directly:

```sql
select * from lookup_vehicle_geography(-122.3321, 47.6062);
select * from lookup_vehicle_geography(-74.0060, 40.7128);
```

## Failure Semantics

- Invalid station and vehicle records are feed-tagged and quarantined while other valid records can continue; invalid required singleton system metadata fails validation for the run.
- Missing, invalid, stale, or unexpectedly future source timestamps are assessed independently for every feed and produce at least a warning.
- Zero valid vehicles, station definitions, or station statuses produces `FAILED`; the station feeds cannot be silently treated as optional.
- Related-station rejections add a warning reason, and a more severe feed result always wins when feed qualities are merged.
- Vehicle volume drops use the configured warning/failure policy but cannot downgrade an existing warning or failure.
- `WARNING` snapshots atomically reconcile all current system/station/vehicle state and grid aggregates while retaining reasons and per-feed evidence.
- A quality-`FAILED` snapshot that completed validation may write vehicle detail, station-status detail, feed metrics, and quarantine evidence, but it cannot replace any current system/station/vehicle state or grid aggregates.
- An extraction or envelope exception aborts transformation/loading; when PostgreSQL is already connected, the pipeline attempts to record a sanitized failed-run row.
- Database write failures roll back the complete snapshot transaction.
- If PostgreSQL is unreachable, the workflow log and nonzero exit code are the only available failure evidence.

## Capacity and Operational Safety

At roughly 13,000 vehicles every 15 minutes, the balanced retention defaults are expected to use approximately 0.7–1.0 GB including indexes. Station-status history and four small feed-metric rows per run add comparatively little volume. Reduce vehicle detail retention before aggregate retention if capacity is constrained: multi-week grid aggregates are necessary for weekday/time baselines, while station-status history shares that aggregate retention window.

Use a schema-owner login only for migrations. Use a member of `fleet_ingest` for the scheduled job and a different member of `fleet_reporting` for Power BI. Credentials and role passwords belong in a secret manager, never in SQL files or Git.

The detailed migration, verification, sizing, troubleshooting, and rollback procedures are in [the operations runbook](docs/operations.md).

## Scope

This repository covers four related Lime GBFS sources, validation and quarantine, PostgreSQL persistence, operational quality, bounded retention, least-privilege access, and stable reporting views. Alert delivery, trip inference, predictive demand modeling, and a deployed Power BI artifact are intentionally out of scope.
