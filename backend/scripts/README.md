<div align="center">
  <img src="../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Backend Utility Scripts
</div>

This directory contains utility scripts for OpenTranscribe backend operations.

## Scripts Overview

### Database & Development Scripts

- **`db_inspect.py`** - Database inspection utility for debugging
  - Usage: `python scripts/db_inspect.py`
  - Inspect current schema and table state

- **`query_tags.py`** - Database query utility for tag debugging
  - Usage: `python scripts/query_tags.py`

- **`fix_error_status.py`** - Fix files stuck in error state
  - Usage: `python scripts/fix_error_status.py`

### Infrastructure Setup Scripts

- **`create_minio_bucket.py`** - Creates MinIO bucket for file storage
  - Usage: `python scripts/create_minio_bucket.py`
  - Creates the required bucket if not auto-created; usually not needed (startup handles this)

- **`create_opensearch_indexes.py`** - Creates OpenSearch indexes for search functionality
  - Usage: `python scripts/create_opensearch_indexes.py`
  - Usually not needed — the backend startup runner creates indexes automatically

### Benchmarking Scripts

- **`benchmark_migration.py`** - Benchmark speaker embedding migration performance
- **`benchmark_queries.py`** - Benchmark database query performance
- **`compare_benchmarks.py`** - Compare benchmark results between runs

### Operational Scripts

- **`blackwell_patches.py`** - NVIDIA Blackwell GPU compatibility patches
- **`retry_youtube_auth_errors_staggered.py`** - Retry YouTube downloads that failed due to auth errors

## Script Categories

### Development & Debugging
- `db_inspect.py` — Database state inspection
- `query_tags.py` — Tag system debugging
- `fix_error_status.py` — Fix stuck error-state files

### Infrastructure Setup
- `create_minio_bucket.py` — Object storage bucket creation (usually auto-handled)
- `create_opensearch_indexes.py` — Search index initialization (usually auto-handled)

### Performance & Operations
- `benchmark_migration.py`, `benchmark_queries.py`, `compare_benchmarks.py` — Performance benchmarking
- `retry_youtube_auth_errors_staggered.py` — Batch retry YouTube auth failures

## Usage Notes

- All scripts should be run from the backend root directory or container
- Scripts use environment variables from `.env` file or container environment
- For Docker deployments, run scripts inside the backend container:
  ```bash
  ./opentr.sh shell backend
  python scripts/script_name.py
  ```

## Dependencies

- Scripts depend on the main application dependencies
- Ensure database, MinIO, and OpenSearch services are running before use
- Some scripts require specific environment variables to be set

## Environment Variables Required

### For MinIO Scripts
```bash
MINIO_SERVER=localhost
MINIO_PORT=9090
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
MINIO_BUCKET_NAME=transcribe-app
```

### For OpenSearch Scripts
```bash
OPENSEARCH_HOST=localhost
OPENSEARCH_PORT=9200
OPENSEARCH_USER=admin
OPENSEARCH_PASSWORD=admin
```

### For Database Scripts
```bash
DATABASE_URL=postgresql://user:pass@host:5432/db
```
