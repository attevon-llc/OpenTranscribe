#!/usr/bin/env bash
# =============================================================================
# Bulk Processing Cheatsheet
# =============================================================================
# Quick reference for monitoring and managing large batch imports
# (e.g., 2500 JRE podcasts across multi-GPU setup)
#
# Usage: Source this file then call any function
#   source scripts/bulk-processing-cheatsheet.sh
#   bulk-dashboard
#   bulk-watch
#
# Database schema notes (media_file table):
#   - upload_time = when file was created/submitted (NOT created_at)
#   - completed_at = when processing finished
#   - file_size = 0 means not yet downloaded (playlist placeholder)
#   - Statuses: PENDING, PROCESSING, COMPLETED, ERROR
# =============================================================================

# Load .env for port/credential defaults
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -f "$PROJECT_DIR/.env" ]]; then
    set -a; source "$PROJECT_DIR/.env"; set +a
fi

PG="docker exec opentranscribe-postgres psql -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-opentranscribe}"

# =============================================================================
# START / STOP
# =============================================================================

alias bulk-start='./opentr.sh start dev --gpu-scale --nas'
alias bulk-stop='./opentr.sh stop'

# =============================================================================
# LIVE PROGRESS
# =============================================================================

# Quick status counts
bulk-status() {
    local offset="${1:-37}"
    $PG -c "SELECT status, count(*) FROM media_file WHERE id > $offset GROUP BY status ORDER BY status;"
}

# Detailed progress with percentages and download breakdown
bulk-progress() {
    local offset="${1:-37}"
    $PG -c "
    SELECT
        count(*) as total,
        count(*) FILTER (WHERE status='COMPLETED') as completed,
        count(*) FILTER (WHERE status='PROCESSING') as processing,
        count(*) FILTER (WHERE status='PENDING') as pending,
        count(*) FILTER (WHERE status='ERROR') as errors,
        count(*) FILTER (WHERE file_size > 0) as downloaded,
        count(*) FILTER (WHERE file_size = 0 OR file_size IS NULL) as not_downloaded,
        ROUND(100.0 * count(*) FILTER (WHERE status='COMPLETED') / NULLIF(count(*), 0), 1) as pct_done
    FROM media_file
    WHERE id > $offset;
    "
}

# Files per hour throughput (rolling window for accuracy)
bulk-throughput() {
    local offset="${1:-37}"
    $PG -c "
    SELECT
        count(*) FILTER (WHERE completed_at IS NOT NULL) as total_completed,
        count(*) FILTER (WHERE completed_at > now() - interval '1 hour') as last_1h,
        count(*) FILTER (WHERE completed_at > now() - interval '3 hours') as last_3h,
        CASE WHEN count(*) FILTER (WHERE completed_at > now() - interval '3 hours') > 0
            THEN ROUND((count(*) FILTER (WHERE completed_at > now() - interval '3 hours')
                  / 3.0)::numeric, 1)
            ELSE NULL
        END as avg_per_hour_3h,
        CASE WHEN count(*) FILTER (WHERE completed_at > now() - interval '1 hour') > 0
            THEN count(*) FILTER (WHERE completed_at > now() - interval '1 hour')
            ELSE NULL
        END as current_rate
    FROM media_file
    WHERE id > $offset AND status = 'COMPLETED';
    "
}

# Hourly completion breakdown
bulk-hourly() {
    local offset="${1:-37}"
    $PG -c "
    SELECT
        to_char(date_trunc('hour', completed_at AT TIME ZONE 'America/New_York'), 'HH12 AM') as hour_est,
        count(*) as completed
    FROM media_file
    WHERE id > $offset AND status = 'COMPLETED' AND completed_at IS NOT NULL
    GROUP BY date_trunc('hour', completed_at AT TIME ZONE 'America/New_York')
    ORDER BY date_trunc('hour', completed_at AT TIME ZONE 'America/New_York');
    "
}

# ETA based on rolling 3-hour throughput (only counts downloaded files as remaining)
bulk-eta() {
    local offset="${1:-37}"
    $PG -c "
    WITH throughput AS (
        SELECT
            count(*) as done_3h,
            3.0 as window_hours
        FROM media_file
        WHERE id > $offset AND status = 'COMPLETED'
          AND completed_at > now() - interval '3 hours'
    ),
    totals AS (
        SELECT
            count(*) FILTER (WHERE status = 'COMPLETED') as total_done,
            count(*) FILTER (WHERE file_size > 0 AND status NOT IN ('COMPLETED', 'ERROR')) as remaining
        FROM media_file WHERE id > $offset
    )
    SELECT
        tot.total_done as completed,
        tot.remaining as remaining,
        CASE WHEN t.done_3h > 0
            THEN ROUND((t.done_3h / t.window_hours)::numeric, 1)
            ELSE NULL
        END as files_per_hour,
        CASE WHEN t.done_3h > 0
            THEN ROUND((tot.remaining / (t.done_3h / t.window_hours))::numeric, 1)
            ELSE NULL
        END as hours_remaining,
        CASE WHEN t.done_3h > 0
            THEN to_char(
                now() + (tot.remaining::numeric / (t.done_3h / t.window_hours)) * interval '1 hour',
                'Day Mon DD HH12:MI AM'
            )
            ELSE NULL
        END as est_completion
    FROM throughput t, totals tot;
    "
}

# Average per-file processing time from task table
bulk-file-timing() {
    local offset="${1:-37}"
    $PG -c "
    SELECT
        count(*) as files,
        ROUND(AVG(EXTRACT(EPOCH FROM (t.completed_at - t.created_at)))::numeric) as avg_secs,
        ROUND(MIN(EXTRACT(EPOCH FROM (t.completed_at - t.created_at)))::numeric) as min_secs,
        ROUND(MAX(EXTRACT(EPOCH FROM (t.completed_at - t.created_at)))::numeric) as max_secs,
        ROUND(AVG(EXTRACT(EPOCH FROM (t.completed_at - t.created_at))) / 60, 1) as avg_mins
    FROM task t
    JOIN media_file mf ON mf.id = t.media_file_id
    WHERE mf.id > $offset AND t.task_type = 'transcription' AND t.status = 'completed';
    "
}

# =============================================================================
# BATCH TIMING (run after completion)
# =============================================================================

bulk-timing() {
    local offset="${1:-37}"
    $PG -c "
    SELECT
        min(upload_time) as first_submitted,
        min(completed_at) FILTER (WHERE status='COMPLETED') as first_completed,
        max(completed_at) FILTER (WHERE status='COMPLETED') as last_completed,
        max(completed_at) FILTER (WHERE status='COMPLETED') - min(upload_time) as total_wall_time,
        count(*) as total_files,
        count(*) FILTER (WHERE status='COMPLETED') as completed,
        count(*) FILTER (WHERE status='ERROR') as errors
    FROM media_file
    WHERE id > $offset;
    "
}

# =============================================================================
# ERROR INVESTIGATION
# =============================================================================

# List failed files (only downloaded ones with real errors)
bulk-errors() {
    local offset="${1:-37}"
    $PG -c "
    SELECT id, LEFT(filename, 50) as filename, file_size,
           LEFT(last_error_message, 60) as error
    FROM media_file
    WHERE id > $offset AND status = 'ERROR' AND file_size > 0
    ORDER BY id;
    "
}

# Count errors by type
bulk-error-summary() {
    local offset="${1:-37}"
    $PG -c "
    SELECT
        CASE
            WHEN file_size = 0 OR file_size IS NULL THEN 'Not downloaded (placeholder)'
            WHEN last_error_message IS NULL OR last_error_message = '' THEN 'No error message (interrupted)'
            WHEN last_error_message ILIKE '%GPU out of memory%' THEN 'GPU OOM'
            WHEN last_error_message ILIKE '%private%' OR last_error_message ILIKE '%unavailable%' THEN 'Private/unavailable'
            WHEN last_error_message ILIKE '%authentication%' OR last_error_message ILIKE '%login%' THEN 'Auth required'
            ELSE 'Other: ' || LEFT(last_error_message, 40)
        END as error_type,
        count(*) as cnt
    FROM media_file
    WHERE id > $offset AND status = 'ERROR'
    GROUP BY error_type
    ORDER BY cnt DESC;
    "
}

# Show error details for a specific file
bulk-error-details() {
    local file_id="${1:?Usage: bulk-error-details <file_id>}"
    $PG -c "SELECT id, filename, status, file_size, source_url, last_error_message FROM media_file WHERE id = $file_id;"
    $PG -c "SELECT id, task_type, status, error_message, created_at, completed_at FROM task WHERE media_file_id = $file_id ORDER BY created_at DESC LIMIT 5;"
}

# Retry all failed downloaded files (reset to PENDING)
bulk-retry-errors() {
    local offset="${1:-37}"
    local count
    count=$($PG -t -A -c "SELECT count(*) FROM media_file WHERE id > $offset AND status = 'ERROR' AND file_size > 0;")
    echo "Found $count failed downloaded files. Resetting to PENDING..."
    $PG -c "UPDATE media_file SET status = 'PENDING', last_error_message = NULL WHERE id > $offset AND status = 'ERROR' AND file_size > 0;"
    echo "Done. Files will be picked up by the periodic health check or startup recovery."
}

# =============================================================================
# GPU MONITORING
# =============================================================================

# Watch GPU utilization (run in separate terminal)
alias gpu-watch='watch -n 2 nvidia-smi'

# One-shot GPU status
gpu-status() {
    nvidia-smi --query-gpu=index,gpu_name,memory.used,memory.total,utilization.gpu,temperature.gpu \
        --format=csv,noheader,nounits | \
    while IFS=, read -r idx name mem_used mem_total util temp; do
        printf "GPU %s: %-25s VRAM: %5s/%5s MB (%3s%%)  Util: %3s%%  Temp: %s°C\n" \
            "$idx" "$name" "$mem_used" "$mem_total" \
            "$((mem_used * 100 / mem_total))" "$util" "$temp"
    done
}

# =============================================================================
# WORKER MONITORING
# =============================================================================

# Celery worker status via Flower API.
#
# ⚠️ issue #609: /api/workers is a ONE-SHOT snapshot taken at Flower's process
# startup (flower/app.py:98), never refreshed on a timer. A worker that was
# still importing torch/whisperx (or preloading GPU models) when Flower booted
# is absent from the *unrefreshed* endpoint forever — this bulk-processing
# helper exists specifically to watch GPU workers during a big batch, so that
# silent omission is the worst possible place for it. `?refresh=1` is Flower's
# own documented parameter and makes it issue a real broadcast for this
# request (flower/api/workers.py, awaited server-side).
bulk-workers() {
    curl -fsS -u "${FLOWER_USER:-admin}:${FLOWER_PASSWORD:-flower}" \
        --max-time 40 \
        "http://localhost:${FLOWER_PORT:-5175}/flower/api/workers?refresh=1" 2>/dev/null | \
        python3 -c "
import sys, json
try:
    workers = json.load(sys.stdin)
    for name, info in sorted(workers.items()):
        active = len(info.get('active', []))
        processed = info.get('stats', {}).get('total', {})
        total = sum(processed.values()) if processed else 0
        print(f'  {name}: active={active}, total_processed={total}')
except (json.JSONDecodeError, AttributeError, TypeError) as exc:
    print(f'  Flower not ready or unreachable ({exc})')
"
}

# Queue depths (via Redis directly - always works)
bulk-queues() {
    local redis_pass="${REDIS_PASSWORD:-}"
    local auth=""
    [[ -n "$redis_pass" ]] && auth="-a $redis_pass"
    echo "  gpu queue:       $(docker exec opentranscribe-redis redis-cli $auth LLEN gpu 2>/dev/null)"
    echo "  download queue:  $(docker exec opentranscribe-redis redis-cli $auth LLEN download 2>/dev/null)"
    echo "  nlp queue:       $(docker exec opentranscribe-redis redis-cli $auth LLEN nlp 2>/dev/null)"
    echo "  embedding queue: $(docker exec opentranscribe-redis redis-cli $auth LLEN embedding 2>/dev/null)"
    echo "  cpu queue:       $(docker exec opentranscribe-redis redis-cli $auth LLEN cpu 2>/dev/null)"
    echo "  utility queue:   $(docker exec opentranscribe-redis redis-cli $auth LLEN utility 2>/dev/null)"
}

# Container logs (last N lines)
bulk-logs() {
    local service="${1:-celery-worker}"
    local lines="${2:-50}"
    docker logs --tail "$lines" "opentranscribe-$service" 2>&1
}

# =============================================================================
# STORAGE MONITORING
# =============================================================================

bulk-storage() {
    echo "=== Storage Usage ==="
    echo "MinIO (NAS):"
    df -h /mnt/nas/opentranscribe-minio 2>/dev/null | tail -1 | awk '{print "  " $3 " used / " $2 " total (" $5 " full)"}' \
        || echo "  (NAS not mounted)"
    echo "Docker Volumes:"
    docker system df -v 2>/dev/null | grep -E "opentranscribe|VOLUME" | head -10 | sed 's/^/  /'
}

# =============================================================================
# SPEAKER PROFILE MANAGEMENT
# =============================================================================

# Backup speaker profiles before data reset
alias speaker-backup='./scripts/speaker-profiles-backup.sh backup'
alias speaker-restore='./scripts/speaker-profiles-backup.sh restore'
alias speaker-list='./scripts/speaker-profiles-backup.sh list'

# =============================================================================
# FULL DASHBOARD (combine key metrics)
# =============================================================================

bulk-dashboard() {
    local offset="${1:-37}"
    echo "============================================"
    echo "  BULK PROCESSING DASHBOARD"
    echo "  $(date '+%a %b %d %I:%M %p %Z')"
    echo "============================================"
    echo ""
    echo "--- Progress ---"
    bulk-progress "$offset"
    echo ""
    echo "--- Hourly Completions ---"
    bulk-hourly "$offset"
    echo ""
    echo "--- Throughput ---"
    bulk-throughput "$offset"
    echo ""
    echo "--- Per-File Timing ---"
    bulk-file-timing "$offset"
    echo ""
    echo "--- ETA (downloaded files) ---"
    bulk-eta "$offset"
    echo ""
    echo "--- GPU Status ---"
    gpu-status
    echo ""
    echo "--- Queue Depths ---"
    bulk-queues
    echo ""
    echo "--- Error Summary ---"
    bulk-error-summary "$offset"
    echo "============================================"
}

# Watch dashboard (refresh every 30s)
bulk-watch() {
    local offset="${1:-37}"
    watch -n 30 "source $PROJECT_DIR/scripts/bulk-processing-cheatsheet.sh && bulk-dashboard $offset"
}

# =============================================================================
# QUICK REFERENCE
# =============================================================================
if [[ "${1:-}" == "help" ]] || [[ "${1:-}" == "--help" ]]; then
    echo "Bulk Processing Cheatsheet"
    echo "========================="
    echo ""
    echo "Start/Stop:"
    echo "  bulk-start                  Start with gpu-scale + nas overlays"
    echo "  bulk-stop                   Stop all services"
    echo ""
    echo "Progress:"
    echo "  bulk-status [offset]        Quick status counts"
    echo "  bulk-progress [offset]      Detailed progress with download breakdown"
    echo "  bulk-hourly [offset]        Completions per hour"
    echo "  bulk-throughput [offset]    Files/hour throughput"
    echo "  bulk-file-timing [offset]  Avg/min/max per-file processing time"
    echo "  bulk-eta [offset]           ETA for remaining downloaded files"
    echo "  bulk-timing [offset]        Final batch timing (run after done)"
    echo ""
    echo "Errors:"
    echo "  bulk-errors [offset]        List failed downloaded files"
    echo "  bulk-error-summary [offset] Errors grouped by type"
    echo "  bulk-error-details <id>     Show error details + task history"
    echo "  bulk-retry-errors [offset]  Reset failed downloaded files to PENDING"
    echo ""
    echo "Monitoring:"
    echo "  gpu-watch                   Live GPU utilization (watch)"
    echo "  gpu-status                  One-shot GPU status"
    echo "  bulk-workers                Celery worker status via Flower"
    echo "  bulk-queues                 Queue depths via Redis"
    echo "  bulk-logs [service] [N]     Container logs (last N lines)"
    echo "  bulk-storage                Disk usage"
    echo ""
    echo "Dashboard:"
    echo "  bulk-dashboard [offset]     Combined status view"
    echo "  bulk-watch [offset]         Auto-refresh dashboard (30s)"
    echo ""
    echo "Speakers:"
    echo "  speaker-backup              Backup speaker profiles"
    echo "  speaker-restore             Restore speaker profiles"
    echo "  speaker-list                List current profiles"
    echo ""
    echo "Offset: number of pre-existing files to skip (default: 37)"
    echo ""
    echo "Usage: source scripts/bulk-processing-cheatsheet.sh"
    echo "       Then use any command above."
fi
