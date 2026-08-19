---
sidebar_position: 8
title: Operational Runbooks
description: Step-by-step procedures for common production scenarios
---

# Operational Runbooks

This page provides step-by-step procedures for diagnosing and resolving common production issues in OpenTranscribe.

---

## Runbook: Stuck Transcription Tasks

**Symptoms**: Files remain in "Processing" or "Transcribing" status indefinitely. The progress bar stops updating. No new files are being processed.

**Diagnosis**:
1. Open the Flower dashboard at `http://localhost:5175/flower`
2. Check the **Active** tab for tasks that have been running longer than expected (typical transcription takes 1-5 minutes per 10 minutes of audio)
3. Check the backend logs for errors:
   ```bash
   ./opentr.sh logs celery-worker
   ```
4. Query the database for stuck files:
   ```bash
   ./opentr.sh shell backend
   python -c "
   from app.db.session import SessionLocal
   from app.models.media import MediaFile, FileStatus
   db = SessionLocal()
   stuck = db.query(MediaFile).filter(
       MediaFile.status.in_([FileStatus.PROCESSING, FileStatus.TRANSCRIBING])
   ).all()
   for f in stuck:
       print(f'{f.id}: {f.filename} - {f.status} (updated: {f.updated_at})')
   "
   ```

**Resolution**:

*Option A: Use the Admin Recovery Panel (recommended)*

1. Log in as an admin user
2. Navigate to **Admin Panel** > **System** > **Task Recovery**
3. The system automatically detects stuck tasks (files in processing state for longer than the configured timeout)
4. Click **Recover Stuck Tasks** to reset them to a retryable state
5. The built-in `TaskRecoveryService` will clean up partial transcript segments before retrying

*Option B: Manual recovery via API*

1. Call the admin recovery endpoint:
   ```bash
   curl -X POST http://localhost:5174/api/admin/recover-tasks \
     -H "Authorization: Bearer <admin-token>"
   ```

*Option C: Cancel and retry individual files*

1. In the Flower dashboard, revoke the stuck Celery task
2. Update the file status in the database:
   ```bash
   ./opentr.sh shell backend
   python -c "
   from app.db.session import SessionLocal
   from app.models.media import MediaFile, FileStatus
   db = SessionLocal()
   file = db.query(MediaFile).get(<FILE_ID>)
   file.status = FileStatus.UPLOADED
   db.commit()
   "
   ```
3. Reprocess the file from the UI

**Prevention**:
- The `TaskRecoveryService` runs automatically on a schedule and handles most stuck task scenarios
- Configure `TASK_RECOVERY_TIMEOUT_MINUTES` in `.env` to match your expected maximum processing time
- Monitor the Flower dashboard regularly for long-running tasks

---

## Runbook: Queue Backup / Slow Processing

**Symptoms**: New uploads queue up but are not processed. Flower shows a growing number of tasks in the **Reserved** or **Scheduled** state. Processing takes much longer than usual.

**Diagnosis**:
1. Check queue depth in Flower at `http://localhost:5175/flower`
2. Check if the Celery worker is running:
   ```bash
   docker compose ps celery-worker
   ```
3. Check worker logs for errors:
   ```bash
   ./opentr.sh logs celery-worker
   ```
4. Check Redis connectivity (the message broker):
   ```bash
   docker compose exec redis redis-cli ping
   # Should respond: PONG
   ```
5. Check GPU utilization (if applicable):
   ```bash
   docker compose exec celery-worker nvidia-smi
   ```

**Resolution**:

*Clear stuck tasks first:*

1. Follow the "Stuck Transcription Tasks" runbook above to clear any blocked tasks

*Scale workers if processing is just slow:*

2. For multi-GPU systems, enable GPU scaling:
   ```bash
   # In .env:
   GPU_SCALE_ENABLED=true
   GPU_SCALE_DEVICE_ID=2
   GPU_SCALE_WORKERS=4

   # Restart with scaling
   ./opentr.sh stop
   ./opentr.sh start dev --gpu-scale
   ```

*Restart the worker if it is unhealthy:*

3. Restart only the Celery worker:
   ```bash
   docker compose restart celery-worker
   ```

*Flush the queue as a last resort (WARNING: loses queued tasks):*

4. Purge all pending Celery tasks:
   ```bash
   docker compose exec celery-worker celery -A app.core.celery purge -f
   ```

**Prevention**:
- Monitor queue depth via Flower
- Scale GPU workers to match throughput needs
- Set up alerts on queue depth thresholds

---

## Runbook: GPU Out of Memory (OOM)

**Symptoms**: Celery worker crashes or restarts unexpectedly. Logs show `CUDA out of memory`, `RuntimeError: CUDA error: out of memory`, or the container is killed by the OOM killer.

**Diagnosis**:
1. Check worker logs for OOM errors:
   ```bash
   ./opentr.sh logs celery-worker | grep -i "out of memory\|OOM\|CUDA error"
   ```
2. Check current GPU memory usage:
   ```bash
   docker compose exec celery-worker nvidia-smi
   ```
3. Check if multiple workers are competing for GPU memory:
   ```bash
   docker compose ps | grep celery
   ```

**Resolution**:

*Step 1: Reduce concurrency*

Lower the number of parallel workers to reduce simultaneous GPU memory usage:
```bash
# In .env:
GPU_SCALE_WORKERS=2  # Reduce from 4 to 2
```
Restart the worker after changing this value.

*Step 2: Use a smaller Whisper model*

Switch from `large-v3` (10GB VRAM) to `large-v3-turbo` (6GB VRAM):
```bash
# In .env:
WHISPER_MODEL=large-v3-turbo
```
Restart the worker. Note: `large-v3-turbo` does not support translation tasks.

*Step 3: Check for GPU memory leaks*

If memory usage grows over time without releasing:
```bash
# Restart the worker to free all GPU memory
docker compose restart celery-worker

# Monitor memory usage over time
watch -n 5 'docker compose exec celery-worker nvidia-smi'
```

*Step 4: Limit Docker GPU memory (advanced)*

In `docker-compose.yml`, add memory limits to the worker:
```yaml
deploy:
  resources:
    reservations:
      devices:
        - capabilities: [gpu]
          device_ids: ['0']
```

**Prevention**:
- Match `GPU_SCALE_WORKERS` to your GPU's VRAM capacity
- Use `large-v3-turbo` for GPUs with less than 10GB VRAM
- Monitor GPU memory with `nvidia-smi` after deployments
- Avoid running other GPU workloads on the same device

---

## Runbook: Database Connection Issues

**Symptoms**: Backend returns 500 errors. Logs show `connection refused`, `too many connections`, or `connection pool exhausted`. API requests hang or timeout.

**Diagnosis**:
1. Check if PostgreSQL is running:
   ```bash
   docker compose ps postgres
   ```
2. Test database connectivity:
   ```bash
   docker compose exec postgres pg_isready -U opentranscribe
   ```
3. Check current connection count:
   ```bash
   docker compose exec postgres psql -U opentranscribe -c \
     "SELECT count(*) FROM pg_stat_activity;"
   ```
4. Check backend logs for connection errors:
   ```bash
   ./opentr.sh logs backend | grep -i "connection\|pool\|database"
   ```

**Resolution**:

*If PostgreSQL is down:*

```bash
docker compose restart postgres
# Wait for it to become healthy
docker compose exec postgres pg_isready -U opentranscribe
# Then restart the backend to re-establish connections
docker compose restart backend
```

*If connection pool is exhausted:*

1. Increase the pool size in `.env`:
   ```bash
   DATABASE_POOL_SIZE=20       # Default is 5
   DATABASE_MAX_OVERFLOW=30    # Default is 10
   ```
2. Restart the backend:
   ```bash
   docker compose restart backend
   ```

*If connections are leaking (count keeps growing):*

1. Kill idle connections:
   ```bash
   docker compose exec postgres psql -U opentranscribe -c \
     "SELECT pg_terminate_backend(pid)
      FROM pg_stat_activity
      WHERE state = 'idle'
      AND query_start < now() - interval '10 minutes';"
   ```
2. Restart the backend to get fresh connections:
   ```bash
   docker compose restart backend
   ```

**Prevention**:
- Monitor active connection count
- Set appropriate `DATABASE_POOL_SIZE` for your workload
- Ensure the backend properly closes sessions (SQLAlchemy context managers)

---

## Runbook: OpenSearch Index Corruption

**Symptoms**: Search returns no results or incomplete results. Admin panel shows index health as "red" or "yellow". Logs show `index_not_found_exception` or shard allocation errors.

**Diagnosis**:
1. Check cluster health:
   ```bash
   curl -s http://localhost:5180/_cluster/health | python3 -m json.tool
   ```
2. Check index status:
   ```bash
   curl -s http://localhost:5180/_cat/indices?v
   ```
3. Check for unassigned shards:
   ```bash
   curl -s http://localhost:5180/_cat/shards?v | grep UNASSIGNED
   ```
4. Check the speaker alias:
   ```bash
   curl -s http://localhost:5180/_cat/aliases?v
   ```

**Resolution**:

*For missing or corrupt transcription index:*

1. Trigger a full reindex from the Admin Panel:
   - Navigate to **Admin Panel** > **System** > **Search**
   - Click **Reindex All Documents**
   - This rebuilds the search index from the database (source of truth)

2. Or via API:
   ```bash
   curl -X POST http://localhost:5174/api/admin/reindex \
     -H "Authorization: Bearer <admin-token>"
   ```

*For unassigned shards:*

```bash
# Retry shard allocation
curl -X POST http://localhost:5180/_cluster/reroute?retry_failed=true

# If single-node, ensure replica count is 0
curl -X PUT http://localhost:5180/_settings -H 'Content-Type: application/json' -d '
{
  "index.number_of_replicas": 0
}'
```

*For corrupt speaker index:*

The speaker index uses an alias-based architecture (`speakers` alias pointing to `speakers_v3` or `speakers_v4`). To rebuild:

1. Delete and recreate via the Admin Panel embedding migration tool
2. Or manually:
   ```bash
   # Check which index the alias points to
   curl -s http://localhost:5180/_cat/aliases/speakers?v

   # Delete the corrupt index
   curl -X DELETE http://localhost:5180/speakers_v3

   # Restart the backend - it will recreate indices on startup
   docker compose restart backend
   ```

**Prevention**:
- Run OpenSearch on reliable storage (avoid network-mounted volumes for production)
- Monitor cluster health regularly
- Keep regular database backups (the database is the source of truth, not OpenSearch)

---

## Runbook: Full Disk Space

**Symptoms**: Uploads fail. Container logs show `No space left on device`. Docker commands fail. Services crash and cannot restart.

**Diagnosis**:
1. Check overall disk usage:
   ```bash
   df -h /
   ```
2. Check Docker disk usage:
   ```bash
   docker system df
   ```
3. Check large directories:
   ```bash
   du -sh /var/lib/docker/*
   du -sh ./models/*
   du -sh ./backups/*
   ```

**Resolution**:

*Step 1: Clean Docker resources*

```bash
# Remove unused containers, networks, and dangling images
docker system prune -f

# Remove unused volumes (WARNING: deletes orphaned data volumes)
docker volume prune -f

# Remove old images
docker image prune -a -f --filter "until=168h"
```

*Step 2: Clean temporary files*

```bash
# Clean backend temp files
docker compose exec backend rm -rf /tmp/yt-dlp-* /tmp/whisperx-*

# Check MinIO for orphaned files
# (files in storage but not referenced in the database)
```

*Step 3: Archive old backups*

```bash
# List backups sorted by size
ls -lhS backups/

# Remove backups older than 30 days
find backups/ -name "*.sql" -mtime +30 -delete
```

*Step 4: Move model cache to larger disk*

```bash
# In .env, change MODEL_CACHE_DIR to a path on a larger disk
MODEL_CACHE_DIR=/mnt/large-disk/opentranscribe-models

# Fix permissions and restart
./scripts/fix-model-permissions.sh
./opentr.sh stop && ./opentr.sh start dev
```

**Prevention**:
- Monitor disk usage with alerts at 80% and 90% thresholds
- Set up automated backup rotation
- Use a separate volume for Docker storage and model cache
- Periodically run `docker system prune`

---

## Runbook: Service Won't Start

**Symptoms**: Running `./opentr.sh start dev` fails. One or more containers exit immediately or enter a restart loop. The frontend or backend is unreachable.

**Diagnosis**:
1. Check which services are failing:
   ```bash
   ./opentr.sh status
   docker compose ps -a
   ```
2. Check logs for the failing service:
   ```bash
   ./opentr.sh logs backend
   ./opentr.sh logs frontend
   ./opentr.sh logs postgres
   ```
3. Check for port conflicts:
   ```bash
   # Check if ports are already in use
   ss -tlnp | grep -E '5173|5174|5175|5179|5180'
   ```
4. Verify the `.env` file exists and is valid:
   ```bash
   # Check for syntax errors (no spaces around =, no missing quotes)
   cat .env | grep -n "= \|^ \|^\t"
   ```
5. Check Docker daemon is running:
   ```bash
   docker info
   ```

**Resolution**:

*If a port is in use:*

```bash
# Find and kill the process using the port
sudo kill $(lsof -t -i:5174)

# Or change the port in .env
BACKEND_PORT=5184
```

*If the backend fails on startup (migration error, missing env var):*

```bash
# Check the specific error in logs
./opentr.sh logs backend | tail -50

# Common fix: ensure .env has all required variables
diff .env .env.example
```

*If containers keep restarting:*

```bash
# Check exit codes
docker compose ps -a

# Check for resource constraints
docker stats --no-stream
```

*If Docker daemon issues:*

```bash
sudo systemctl restart docker
./opentr.sh stop && ./opentr.sh start dev
```

**Prevention**:
- Keep `.env` in sync with `.env.example` when upgrading
- Test configuration changes in development before production
- Use `./opentr.sh status` after starting to verify all services are healthy

---

## Runbook: Failed Database Migration

**Symptoms**: Backend fails to start with Alembic errors. Logs show `alembic.util.exc.CommandError`, `Can't locate revision`, or `Target database is not up to date`.

**Diagnosis**:
1. Check backend logs for the specific migration error:
   ```bash
   ./opentr.sh logs backend | grep -i "alembic\|migration\|revision"
   ```
2. Check the current database revision:
   ```bash
   ./opentr.sh shell backend
   alembic current
   ```
3. Check migration history:
   ```bash
   alembic history --verbose
   ```

**Resolution**:

*If the database has no Alembic stamp (fresh or legacy database):*

The backend's `migrations.py` auto-detection handles this on startup. If it fails:
```bash
./opentr.sh shell backend
# Stamp the database at the appropriate version
alembic stamp <current_version>
# Then upgrade
alembic upgrade head
```

*If a migration partially applied (some columns exist, some don't):*

1. Check what was applied:
   ```bash
   ./opentr.sh shell postgres
   psql -U opentranscribe -c "\d+ <table_name>"
   ```
2. Manually complete the migration or stamp past it:
   ```bash
   ./opentr.sh shell backend
   # If the schema is correct but stamp is wrong
   alembic stamp <target_revision>
   ```

*If you need to rollback a migration:*

```bash
./opentr.sh shell backend
# Downgrade one revision
alembic downgrade -1

# Or downgrade to a specific revision
alembic downgrade <revision_id>
```

*Nuclear option (development only -- destroys all data):*

```bash
./opentr.sh reset dev
```

**Prevention**:
- Always use idempotent SQL in migrations (`IF NOT EXISTS`, `IF EXISTS`)
- Test migrations with `./opentr.sh reset dev` before deploying
- Back up the database before applying new migrations in production:
  ```bash
  ./opentr.sh backup
  ```

---

## Runbook: High Memory Usage

**Symptoms**: System becomes sluggish. Docker containers are killed by the OOM killer. `docker stats` shows high memory consumption. Swap usage is high.

**Diagnosis**:
1. Check per-container memory usage:
   ```bash
   docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}"
   ```
2. Identify the largest consumer:
   ```bash
   docker stats --no-stream --format "{{.Name}}\t{{.MemUsage}}" | sort -k2 -h
   ```
3. Check host system memory:
   ```bash
   free -h
   ```

**Resolution**:

*If OpenSearch is consuming too much memory:*

OpenSearch defaults can be aggressive. Reduce the JVM heap:
```bash
# In docker-compose.yml or .env:
OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g  # Default is 4g
```

1 GB is the measured floor for the default embedding model — below it a model reports
`DEPLOY: COMPLETED` and then fails to produce an embedding. See
[Performance Tuning](./performance-tuning.md#opensearch-heap-what-it-is-actually-for).
Restart OpenSearch after changing.

*If the Celery worker is consuming too much:*

This is usually due to model loading. Each model stays in memory:
```bash
# Restart the worker to free memory
docker compose restart celery-worker
```

*If PostgreSQL is consuming too much:*

Reduce shared buffers:
```bash
# In docker-compose.yml postgres environment:
POSTGRES_SHARED_BUFFERS=256MB  # Reduce from default
```

*Set container memory limits:*

Add limits in `docker-compose.yml`:
```yaml
services:
  backend:
    deploy:
      resources:
        limits:
          memory: 2G
  opensearch:
    deploy:
      resources:
        limits:
          memory: 2G
```

**Prevention**:
- Set memory limits on all containers
- Monitor memory usage trends over time
- Size your server appropriately (16GB minimum recommended, 32GB for production)

---

## Runbook: Credential Rotation

**Symptoms**: Planned maintenance -- you need to rotate credentials for security compliance or after a suspected compromise.

**Diagnosis**: Not applicable (proactive maintenance).

**Resolution**:

### Rotate Database Password

1. Back up the database first:
   ```bash
   ./opentr.sh backup
   ```
2. Update the password in PostgreSQL:
   ```bash
   docker compose exec postgres psql -U opentranscribe -c \
     "ALTER USER opentranscribe WITH PASSWORD 'new_secure_password';"
   ```
3. Update `.env`:
   ```bash
   DATABASE_URL=postgresql://opentranscribe:new_secure_password@postgres:5432/opentranscribe
   ```
4. Restart services that connect to the database:
   ```bash
   docker compose restart backend celery-worker
   ```

### Rotate MinIO Keys

1. Update MinIO credentials via the MinIO console at `http://localhost:5179`, or:
   ```bash
   docker compose exec minio mc admin user svcacct add myminio opentranscribe \
     --access-key NEW_ACCESS_KEY --secret-key NEW_SECRET_KEY
   ```
2. Update `.env`:
   ```bash
   MINIO_ACCESS_KEY=NEW_ACCESS_KEY
   MINIO_SECRET_KEY=NEW_SECRET_KEY
   ```
3. Restart services:
   ```bash
   docker compose restart backend celery-worker
   ```

### Rotate JWT Secret

1. Update `.env`:
   ```bash
   JWT_SECRET_KEY=$(openssl rand -hex 32)
   ```
2. Restart the backend:
   ```bash
   docker compose restart backend
   ```
3. **Note**: All existing user sessions will be invalidated. Users will need to log in again.

### Rotate LLM API Keys

1. Generate a new key from your LLM provider's dashboard
2. Update `.env`:
   ```bash
   LLM_API_KEY=new_api_key_here
   ```
3. Or update via the Admin Panel: **Settings** > **LLM Configuration** > **API Key**
4. Restart the backend:
   ```bash
   docker compose restart backend celery-worker
   ```

### Rotate Redis Password

1. Update `.env`:
   ```bash
   REDIS_PASSWORD=new_redis_password
   CELERY_BROKER_URL=redis://:new_redis_password@redis:6379/0
   ```
2. Restart all services that use Redis:
   ```bash
   docker compose restart redis backend celery-worker
   ```

**Prevention**:
- Rotate credentials on a regular schedule (e.g., every 90 days)
- Use strong, randomly generated passwords (`openssl rand -hex 32`)
- Document credential rotation dates
- Never commit credentials to version control

---

## Runbook: Model Re-Download

**Symptoms**: AI models are corrupted, producing garbled output, or you need to switch to a different model version. Or you want to force a fresh download after a failed partial download.

**Diagnosis**:
1. Check current model cache:
   ```bash
   ls -la ${MODEL_CACHE_DIR:-./models}/huggingface/hub/
   ls -la ${MODEL_CACHE_DIR:-./models}/torch/pyannote/
   ```
2. Check for incomplete downloads (very small files, lock files):
   ```bash
   find ${MODEL_CACHE_DIR:-./models} -name "*.lock" -o -name "*.incomplete"
   ```

**Resolution**:

*Re-download a specific model type:*

```bash
# Clear Whisper models
rm -rf ${MODEL_CACHE_DIR:-./models}/huggingface/hub/models--Systran--faster-whisper-*

# Clear PyAnnote speaker models
rm -rf ${MODEL_CACHE_DIR:-./models}/torch/pyannote/

# Clear sentence transformer models
rm -rf ${MODEL_CACHE_DIR:-./models}/sentence-transformers/

# Clear OpenSearch neural models
rm -rf ${MODEL_CACHE_DIR:-./models}/opensearch-ml/
```

*Re-download all models:*

```bash
# Stop services
./opentr.sh stop

# Clear the entire model cache
rm -rf ${MODEL_CACHE_DIR:-./models}/*

# Fix permissions
./scripts/fix-model-permissions.sh

# Restart -- models will download on first use
./opentr.sh start dev
```

*Pre-download models for offline deployment:*

```bash
bash scripts/download-models.sh models
```

**Prevention**:
- Verify model downloads completed successfully after first startup
- Use stable network connections for initial model downloads
- For air-gapped environments, pre-download models on an internet-connected machine and transfer via `rsync`

---

## Runbook: WebSocket Connection Issues

**Symptoms**: The UI does not update in real-time after uploading files. Progress bars do not animate. Transcription completes but the UI still shows "Processing". Browser console shows WebSocket errors.

**Diagnosis**:
1. Check browser developer console (F12) for WebSocket errors:
   - Look for `WebSocket connection to 'ws://...' failed`
   - Look for `ERR_CONNECTION_REFUSED` or `403 Forbidden`
2. Verify the backend WebSocket endpoint is reachable:
   ```bash
   # Test with wscat (install: npm install -g wscat)
   wscat -c ws://localhost:5174/api/ws
   ```
3. Check backend logs for WebSocket errors:
   ```bash
   ./opentr.sh logs backend | grep -i "websocket\|ws\|upgrade"
   ```
4. If using NGINX (production), check NGINX logs:
   ```bash
   ./opentr.sh logs frontend | grep -i "websocket\|upgrade"
   ```

**Resolution**:

*If running in development (no NGINX):*

1. Verify the backend is running and healthy:
   ```bash
   curl http://localhost:5174/api/health
   ```
2. Restart the backend:
   ```bash
   docker compose restart backend
   ```
3. Hard-refresh the browser (`Ctrl+Shift+R`)

*If running in production (with NGINX):*

1. Verify NGINX is configured for WebSocket proxying. The configuration should include:
   ```nginx
   location /api/ws {
       proxy_pass http://backend:8080;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_set_header Host $host;
       proxy_read_timeout 86400;
   }
   ```
2. Check that `proxy_read_timeout` is set high enough (WebSocket connections are long-lived)
3. Restart NGINX:
   ```bash
   docker compose restart frontend
   ```

*If WebSocket connects but no updates arrive:*

1. Check that the Celery worker is sending notifications:
   ```bash
   ./opentr.sh logs celery-worker | grep -i "notification\|websocket\|send"
   ```
2. Check Redis pub/sub (used for WebSocket message passing):
   ```bash
   docker compose exec redis redis-cli ping
   ```

**Prevention**:
- In production, always use the provided NGINX configuration which includes WebSocket support
- Set `proxy_read_timeout` to at least 86400 (24 hours) for WebSocket connections
- Monitor WebSocket connection counts in production
- Test WebSocket functionality after any NGINX configuration changes
