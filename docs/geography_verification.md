# Geography Enrichment Verification

Verification was run on 2026-09-12 against the configured Supabase PostgreSQL database after applying `008_geography_enrichment.sql`. Migration 008 and the loader were each executed twice successfully to verify rerun safety. Migration `009_geography_refresh_status.sql` was subsequently applied to expose the stored update timestamps.

## Source inspection and download

| Source | HTTP | Bytes | Features | Properties | Input geometry types | Repaired | Loaded |
|---|---:|---:|---:|---|---|---:|---:|
| Neighborhoods | 200 | 12,494,190 | 175 | `area`, `city`, `county`, `name`, `nested`, `nhood` | `Polygon`, `MultiPolygon` | 57 | 90 |
| ZIP codes | 200 | 49,044 | 32 | `AFFGEOID10`, `ALAND10`, `AWATER10`, `GEOID10`, `ZCTA5CE10` | `Polygon` | 0 | 32 |
| Council districts | 200 | 90,188 | 7 | `district` | `Polygon` | 0 | 7 |

All 175 neighborhood features were geometry-validated. The 85 records whose `city` property was not `Seattle` were explicitly counted and excluded after validation. All 57 invalid source polygons were logged, repaired, normalized to `MultiPolygon`, and revalidated; no invalid feature was silently discarded.

## Database results

| Relation | Rows | Invalid geometry | Wrong SRID |
|---|---:|---:|---:|
| `dim_neighborhood` | 90 | 0 | 0 |
| `dim_zip_area` | 32 | 0 | 0 |
| `dim_council_district` | 7 | 0 | 0 |
| `dim_grid` | 406 | 0 | 0 |

The current stored `last_successful_update` values are:

| Relation | Last successful update (UTC) |
|---|---|
| `dim_neighborhood` | `2026-09-13 04:56:42.836059+00` |
| `dim_zip_area` | `2026-09-13 04:56:42.836059+00` |
| `dim_council_district` | `2026-09-13 04:56:42.836059+00` |
| `dim_grid` | `2026-09-13 04:56:43.864136+00` |

The following GiST indexes were present:

- `idx_dim_neighborhood_geometry`
- `idx_dim_zip_area_geometry`
- `idx_dim_council_district_geometry`
- `idx_dim_grid_geometry`

## Point-in-polygon checks

Downtown Seattle:

```text
longitude=-122.3321 latitude=47.6062
neighborhood_name=Central Business District
zip_code=98164
council_district_name=District 7
grid_id=GRID_13760_05766
```

Outside Seattle:

```text
longitude=-74.0060 latitude=40.7128
neighborhood_name=NULL
zip_code=NULL
council_district_name=NULL
grid_id=NULL
```

These checks are part of `scripts/ingest_geography.py`; a future load fails and rolls back if a dimension is empty, any stored geometry is invalid or not SRID 4326, a spatial index is missing, the Seattle sample has no match, or the outside sample gains a match.
