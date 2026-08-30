# Real-Time Fleet Operations Intelligence

This repository is a small, end-to-end data system for observing Lime's Seattle
micromobility fleet. Every run downloads the current public GBFS vehicle feed,
checks whether the data is usable, converts it into a stable internal shape,
stores it in PostgreSQL, and exposes reporting views that Power BI can query.

The system is a **scheduled snapshot pipeline**, not streaming. A scheduled job
takes a fresh snapshot twice daily at 9:00 AM and 9:00 PM Pacific Time. That
distinction matters: the source publishes the state visible at one moment, not a
continuous event for every trip or movement.

## Why This System Exists

A raw API response is useful to a program, but it is not yet trustworthy or
convenient for an operations dashboard. Several problems have to be solved first:

1. The source can be unavailable, slow, malformed, stale, or unexpectedly small.
2. Individual records can have missing IDs, invalid coordinates, or duplicates.
3. Field names used by the source should not leak into every downstream report.
4. An operations dashboard needs the latest observed state, while trend analysis needs history.
5. Detailed history grows too quickly to retain forever in a small database.
6. Failed runs and rejected records must remain visible instead of disappearing.

This repository addresses those problems as one pipeline:

```text
Lime Seattle GBFS API
        |
        | HTTPS JSON snapshot
        v
extract -> validate -> assess quality -> transform
        |
        | one PostgreSQL transaction
        v
current state + recent detail + 15-minute aggregates + audit records
        |
        | reporting views
        v
Power BI DirectQuery
```

GitHub Actions supplies the Pacific-time-aware clock that starts this process at
9:00 AM and 9:00 PM each day.
PostgreSQL is the system of record. Power BI is deliberately the presentation
layer, not the place where source data is cleaned or core business rules are
reimplemented.

## How One Run Works

The executable entry point is `python -m src.main`. One run proceeds as follows:

1. `src/config.py` loads `.env` and validates configuration.
2. `src/load.py` opens PostgreSQL and reads recent normal record volumes.
3. `src/extract.py` requests the current GBFS JSON with timeout and retry logic.
4. `src/validate.py` checks the payload and separates accepted and rejected rows.
5. `src/metrics.py` fails empty snapshots and detects missing/stale timestamps or
   an unusual drop in vehicle count without allowing warnings to downgrade failures.
6. `src/transform.py` creates stable database rows and geographic grid IDs.
7. `src/load.py` transactionally writes detail, synchronizes the full current
   snapshot, replaces affected aggregate buckets, updates reporting thresholds,
   quarantines rejected rows, applies retention, and records the run.
8. `src/main.py` updates the run record after commit so pipeline duration includes
   extraction, processing, database writes, and retention work.

The process exits with a nonzero status on failure. That status is how GitHub
Actions knows to mark a scheduled run as failed.

## Repository Map: Why Every File Is Here

### Root files

| File | What it does | Why it is needed | Place in the system |
|---|---|---|---|
| `README.md` | Documents the system, setup, operation, and design. | A data pipeline is not maintainable if a new person cannot reconstruct how its pieces interact. | Human entry point to the repository. |
| `requirements.txt` | Pins every direct third-party dependency: `requests`, `psycopg` with its binary driver, and `pytest`. | Python must install compatible HTTP, PostgreSQL, and test libraries in local and CI environments; pip installs their transitive dependencies automatically. | Reproducible runtime and test environment. |
| `.env.example` | Lists every supported setting without real credentials. | It defines the configuration contract while keeping secrets out of Git. | Template for local configuration and GitHub secrets. |
| `.gitignore` | Excludes `.env`, virtual environments, bytecode, and caches. | Credentials and generated machine-specific files must not enter version control. | Repository hygiene and secret protection. |
| `.env` | Holds this machine's actual local settings and database credentials. | Local commands need configuration without repeatedly setting shell variables. | Loaded automatically by `src/config.py`; intentionally ignored by Git. |

### Automation

| File | What it does | Why it is needed | Place in the system |
|---|---|---|---|
| `.github/workflows/ingest.yml` | Creates Python on a GitHub runner, installs dependencies, runs tests, then runs ingestion at 9:00 AM and 9:00 PM Pacific or on demand. | The pipeline needs an external scheduler and failures need a visible execution history. | Production orchestration; GitHub secrets become environment variables here. |

### Python package

| File | What it does | Why it is needed | Place in the system |
|---|---|---|---|
| `src/__init__.py` | Marks `src` as an importable Python package. | Module commands such as `python -m src.main` and relative imports need a package boundary. | Package plumbing. |
| `src/config.py` | Reads `.env` and environment variables, converts text to typed settings, and rejects missing or invalid configuration. | All modules need one consistent source for URLs, credentials, thresholds, geography, and retention. | First runtime step and configuration boundary. |
| `src/extract.py` | Calls Lime's GBFS endpoint, retries transient failures, parses JSON, and records HTTP latency. | External networks fail in ordinary ways; extraction must fail clearly and expose useful timing data. | Source boundary: remote JSON enters the system here. |
| `src/validate.py` | Verifies feed structure and timestamps, normalizes source field names, validates coordinates and state, detects duplicate IDs, and records rejection reasons. | Untrusted source data must not silently corrupt current state or reporting history. | Quality gate between extraction and transformation. |
| `src/metrics.py` | Evaluates feed freshness and volume anomalies, rounds timestamps to 15-minute buckets, and creates grid aggregates. | Record validity cannot detect a technically valid but suspicious snapshot; compact aggregates also make history affordable. | Snapshot-level quality and analytical aggregation. |
| `src/transform.py` | Derives availability, assigns deterministic rectangular grid IDs, and adds timestamps, source labels, quality status, and run lineage. | Downstream tables need a stable business shape independent of GBFS naming details. | Converts accepted source records into load-ready records. |
| `src/load.py` | Connects to PostgreSQL, reads the recent baseline, synchronizes current state, replaces aggregate buckets, updates reporting thresholds, logs runs, and applies retention transactionally. | Database behavior belongs in one module so writes are atomic, parameterized, idempotent, and storage remains bounded. | Persistence boundary between Python and PostgreSQL. |
| `src/main.py` | Coordinates one complete production run and provides a no-write dry-run mode. | A scheduler needs one command with a clear success or failure exit status. | Application entry point. |
| `src/self_test.py` | Runs built-in checks against local logic, the live Lime endpoint, the dry pipeline, and the configured PostgreSQL connection. | Unit tests cannot prove that today's external API or credentials work. | Integration and operator diagnostic tool. |

### Database definition

These files are intentionally ordered migrations. Run them in numerical order
against a new Supabase/PostgreSQL database. Existing deployments should rerun
`002_indexes.sql` and `003_views.sql`, then apply `004_security.sql`; these
operations are idempotent.

| File | What it does | Why it is needed | Place in the system |
|---|---|---|---|
| `sql/001_schema.sql` | Creates tables, constraints, keys, and default reporting thresholds. | Python cannot load data until PostgreSQL has a durable, validated storage model. | Physical data model. |
| `sql/002_indexes.sql` | Adds indexes for current-grid, time-series, rejection, and pipeline-health queries. | Correct tables can still be too slow for repeated DirectQuery access and retention deletes. | Database performance layer. |
| `sql/003_views.sql` | Creates current fleet, grid supply, historical baseline, rebalancing, trend, and pipeline-health views. | Reports need stable business-facing datasets without duplicating SQL inside Power BI. | Semantic/reporting layer consumed by Power BI. |
| `sql/004_security.sql` | Removes Supabase API-role access and enables row-level security on operational tables. | Public API roles must not be able to read, rewrite, truncate, or delete fleet data. | Database security boundary. |

Retention is runtime behavior in `src/load.py`, not a schema migration, because
the retention ages come from configuration and cleanup must run with every
snapshot transaction.

### Automated tests

| File | What it does | Why it is needed | Place in the system |
|---|---|---|---|
| `tests/test_config.py` | Tests `.env` parsing, defaults, overrides, and configuration validation. | A configuration bug can stop the pipeline before it reaches the source or database. | Fast, offline unit coverage for configuration. |
| `tests/test_load.py` | Tests transaction mode, current-state reconciliation, failed-snapshot isolation, and grid-bucket replacement. | Persistence regressions can otherwise report success while leaving incorrect database state. | Fast, offline unit coverage for loading behavior. |
| `tests/test_main.py` | Tests orchestration and final end-to-end runtime measurement. | Run metadata must describe the complete pipeline rather than only pre-load work. | Fast, offline coverage for the application entry point. |
| `tests/test_sql.py` | Guards Seattle-local time semantics, zero-current grid candidates, and required RLS statements. | Reporting and security behavior lives partly in SQL and needs regression coverage. | Static coverage for database definitions. |
| `tests/test_validate.py` | Tests valid rows and rejection cases such as duplicate IDs, missing IDs, and invalid coordinates. | Validation is the main protection against bad source data. | Fast, offline unit coverage for the quality gate. |
| `tests/test_transform.py` | Tests availability rules, grid assignment, and transformed metadata. | Derived fields must remain deterministic because database keys and reports depend on them. | Fast, offline unit coverage for transformation. |

`tests/` and `src/self_test.py` are not duplicates. Pytest uses controlled inputs
and should be fast and deterministic. The self-test intentionally reaches the
live API and database, so it answers a different question: whether the complete
system's external dependencies work right now.

## Database Model From First Principles

One table cannot efficiently serve every use case. The database therefore stores
the same observation at different levels of detail:

| Database object | Grain | Reason it exists |
|---|---|---|
| `current_vehicle_state` | One latest row per vehicle | Fast operational maps and counts without searching history. |
| `fact_vehicle_snapshot` | One vehicle per ingestion run | Recent forensic detail and short-term vehicle-level analysis. |
| `fact_grid_15min` | One geographic grid per 15-minute bucket | Compact long-term trends that do not grow at vehicle-level speed. |
| `rejected_records` | One invalid source record | Data lineage: bad rows remain explainable instead of vanishing. |
| `pipeline_runs` | One pipeline attempt | Operational evidence of freshness, latency, volume, warnings, and failures. |
| `reporting_thresholds` | One named reporting setting | Rebalancing thresholds can change without rewriting a SQL view. |

The current-state table is synchronized from each successful full snapshot: a
known vehicle is replaced, a new vehicle is inserted, and a vehicle absent from
the accepted snapshot is removed. Historical rows use the run ID as part of
their identity so retrying the same run does not create duplicates. Each affected
15-minute grid bucket is replaced as a unit so it cannot mix multiple snapshots.
Failed-quality snapshots remain auditable in detail but cannot replace current
state or long-term aggregates. Environment rebalancing thresholds are synchronized
to `reporting_thresholds` during the same transaction.

The grid is a deterministic rectangular bucket calculated from latitude and
longitude. It is intentionally simple. Its job is to compare observed supply by
area, not to claim that the system has measured trips or predicted demand.

## Setup

### 1. Create the Python environment

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. Configure local settings

Create `.env` beside `README.md`, using `.env.example` as the list of keys. The
loader finds this file from the repository location, so commands do not require
manual environment-variable exports.

Required database settings are:

```dotenv
DB_HOST=your-supabase-host
DB_PORT=5432
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=your-password
```

`GBFS_URL` has a public Seattle default. Blank optional values use their documented
defaults; a blank `FEED_STALE_MINUTES` specifically leaves stale-feed detection
disabled. Real `.env` values are secrets and must never be committed.

### 3. Create the PostgreSQL objects

Open the Supabase SQL Editor and run these files in order:

```text
sql/001_schema.sql
sql/002_indexes.sql
sql/003_views.sql
sql/004_security.sql
```

This is a required one-time setup. A successful connection test proves that the
credentials work; it does not create tables. If a test reports that
`pipeline_runs` does not exist, the schema files have not yet been applied.

For an existing database created with migrations 001-003, rerun `002_indexes.sql`
to add the aggregate-retention index, rerun `003_views.sql`, and then run
`004_security.sql`. The security migration revokes all table/view privileges from
Supabase `anon` and `authenticated` roles and enables RLS without API policies.
Apply it only when clients use the documented direct PostgreSQL connection;
existing Supabase Data API clients will lose access.

### 4. Verify the system

Run deterministic unit tests:

```powershell
python -m pytest
```

Run the operator self-test, including the live API and database connection:

```powershell
python -m src.self_test
```

Useful narrower checks are:

```powershell
python -m src.self_test --skip-live
python -m src.self_test --skip-db
python -m src.self_test --verbose
```

To exercise extract, validation, quality assessment, and transformation without
writing anything to PostgreSQL:

```powershell
$env:FLEET_DRY_RUN="1"
python -m src.main
Remove-Item Env:FLEET_DRY_RUN
```

Use `python -m pytest tests/test_config.py` to run one test file. With Python's
`-m` option, module names do not include `.py`; therefore
`python -m tests.test_config.py` is not a valid command.

### 5. Run one real ingestion

After the schema and `.env` are ready:

```powershell
python -m src.main
```

Run this from the activated `.venv`; alternatively, use
`.\.venv\Scripts\python.exe -m src.main` explicitly. The command prints each
pipeline stage and writes to the configured database. A successful run adds one
row to `pipeline_runs`, adds vehicle-level history, synchronizes current state to
the accepted full snapshot, replaces the affected 15-minute grid bucket, and
updates reporting thresholds. Its reported duration includes database work.

## Configuration Reference

| Variable | Required | Default | Meaning |
|---|---:|---:|---|
| `GBFS_URL` | No | Lime Seattle feed | Source endpoint. |
| `DB_HOST` | Yes | None | PostgreSQL/Supabase hostname. |
| `DB_PORT` | No | `5432` | PostgreSQL port. |
| `DB_NAME` | Yes | None | Database name. |
| `DB_USER` | Yes | None | Database login user. |
| `DB_PASSWORD` | Yes | None | Database login password. |
| `DETAIL_RETENTION_DAYS` | No | `3` | Age at which vehicle snapshots, grid aggregates, and pipeline-run history are deleted. |
| `REJECTED_RETENTION_DAYS` | No | `3` | Age at which rejected raw payloads are deleted. |
| `FEED_STALE_MINUTES` | No | Disabled | Maximum acceptable source age. |
| `VOLUME_DROP_THRESHOLD` | No | `0.50` | Fractional fall below recent normal volume that triggers QA. |
| `VOLUME_DROP_POLICY` | No | `WARNING` | Whether a detected drop becomes `WARNING` or `FAILED`. |
| `SEATTLE_BBOX_MIN_LAT` and companions | No | Disabled | Optional four-coordinate geographic acceptance boundary. |
| `GRID_SIZE_DEGREES` | No | `0.01` | Width and height of the rectangular aggregation grid. |
| `REBALANCE_HIGH_THRESHOLD` | No | `10` | High-priority supply-gap threshold synchronized to PostgreSQL each run. |
| `REBALANCE_MEDIUM_THRESHOLD` | No | `5` | Medium-priority supply-gap threshold synchronized to PostgreSQL each run. |

Shell or CI environment variables take precedence over values in `.env`. This
lets local development use a file while GitHub Actions safely injects secrets.
Blank optional CI values use the documented defaults. Unsafe values such as
negative retention periods, invalid ports, reversed bounding boxes, or inverted
rebalancing thresholds fail during startup before any database mutation.

## GitHub Actions

The workflow runs at `09:00` and `21:00` in the IANA
`America/Los_Angeles` timezone, so daylight-saving transitions are handled
automatically. It can also be started manually with `workflow_dispatch`. Add the
variables from `.env.example` as GitHub Actions secrets. At minimum, add
`DB_HOST`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD`; `DB_PORT` defaults to `5432`.
The workflow fixes both retention values at three days.

The workflow runs pytest before ingestion. This prevents a known failing code
change from writing a new snapshot. GitHub's scheduler is best-effort, so a job
may start slightly after its nominal 9:00 AM or 9:00 PM trigger.

## Power BI

In Power BI Desktop, choose:

```text
Get Data -> PostgreSQL database -> DirectQuery
```

Connect to the same PostgreSQL database and build visuals from these views:

| View | Intended use |
|---|---|
| `vw_current_fleet_summary` | Fleet count, availability, latest source time, and latest pipeline state. |
| `vw_current_grid_supply` | Current geographic supply and availability rate. |
| `vw_grid_supply_baseline` | Typical supply by grid, Seattle-local weekday, and 15-minute time bucket. |
| `vw_rebalancing_priority` | Current supply gap, including historically active grids with zero current vehicles, and availability-based operational priority. |
| `vw_fleet_availability_trend` | Historical grid trends from compact aggregates. |
| `vw_pipeline_health` | Recent success, warning, failure, latency, rejection, and freshness measures. |

Refresh Power BI after the twice-daily ingestion windows. Keep the interpretation
precise: the dashboard shows availability observed at the latest scheduled
snapshot and an availability-based rebalancing signal. It does not prove completed
trips, measure demand directly, or represent continuous live state.

## Failure Semantics

Invalid individual records are quarantined while valid records can continue.
A snapshot with zero valid vehicles always fails. Missing state flags are rejected
rather than interpreted as available, and Lime's `vehicle_type` field is preserved.
A missing/stale timestamp or low-volume snapshot receives the configured quality status. A
`FAILED` quality snapshot is preserved in detailed history and the run log but
does not overwrite live current state or long-term grid aggregates. Snapshot
writes, threshold synchronization, and retention cleanup occur in one transaction
so reporting does not see partially updated state.

Supabase `anon` and `authenticated` roles have no privileges on these tables or
views, and row-level security is enabled as defense in depth. The ETL and Power BI
DirectQuery use the explicitly configured PostgreSQL login.

If the database itself is unavailable, Python cannot record the failure there;
the nonzero process exit and GitHub Actions log are then the authoritative error
record.

## Current Scope

This is an MVP built around one Lime Seattle free-bike-status feed, a rectangular
grid, PostgreSQL, and Power BI DirectQuery. It does not include confirmed trip
events, predictive demand modeling, alert delivery, or a deployed Power BI file.
Those are extensions of the system, not assumptions hidden inside the current
implementation.
