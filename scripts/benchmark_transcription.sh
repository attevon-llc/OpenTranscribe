#!/usr/bin/env bash
# Benchmark end-to-end transcription processing time.
#
# Usage:
#   ./scripts/benchmark_transcription.sh [FILE_UUID]
#
# Defaults to the ~2.35hr test file (JRE #1820 - Jack Carr) if no UUID given.
# Monitors celery-worker logs for TIMING entries and captures wall-clock time
# from reprocess trigger to task completion.
#
# Requirements: curl, jq, docker compose (running environment)

set -euo pipefail

# --- Configuration ---
API_BASE="http://localhost:5174/api"
FILE_UUID="${1:-5132f182-ecf1-48b1-922b-021416506928}"
EMAIL="admin@example.com"
PASSWORD="password"
LOG_FILE="/tmp/benchmark_$(date +%Y%m%d_%H%M%S).log"
TIMING_FILE="/tmp/benchmark_timing_$(date +%Y%m%d_%H%M%S).log"

echo "=============================================="
echo " OpenTranscribe Transcription Benchmark"
echo "=============================================="
echo "File UUID:   $FILE_UUID"
echo "Log file:    $LOG_FILE"
echo "Timing file: $TIMING_FILE"
echo ""

# --- Step 1: Authenticate ---
echo "[$(date +%H:%M:%S)] Authenticating..."
AUTH_RESPONSE=$(curl -s -X POST "$API_BASE/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=$EMAIL&password=$PASSWORD")

TOKEN=$(echo "$AUTH_RESPONSE" | jq -r '.access_token // empty')
if [ -z "$TOKEN" ]; then
  echo "ERROR: Failed to authenticate. Response: $AUTH_RESPONSE"
  exit 1
fi
echo "[$(date +%H:%M:%S)] Authenticated successfully."

# --- Step 2: Get file info ---
echo "[$(date +%H:%M:%S)] Fetching file info..."
FILE_INFO=$(curl -s -X GET "$API_BASE/files/$FILE_UUID" \
  -H "Authorization: Bearer $TOKEN")
FILENAME=$(echo "$FILE_INFO" | jq -r '.filename // "unknown"')
DURATION=$(echo "$FILE_INFO" | jq -r '.duration // 0')
FILE_SIZE=$(echo "$FILE_INFO" | jq -r '.file_size // 0')
DURATION_MIN=$(echo "scale=1; $DURATION / 60" | bc 2>/dev/null || echo "?")
DURATION_HR=$(echo "scale=2; $DURATION / 3600" | bc 2>/dev/null || echo "?")
SIZE_MB=$(echo "scale=0; $FILE_SIZE / 1048576" | bc 2>/dev/null || echo "?")

echo ""
echo "--- Benchmark Target ---"
echo "  File:     $FILENAME"
echo "  Duration: ${DURATION_MIN} min (${DURATION_HR} hrs)"
echo "  Size:     ${SIZE_MB} MB"
echo "  Status:   $(echo "$FILE_INFO" | jq -r '.status')"
echo ""

# --- Step 3: Clear old logs and start monitoring ---
# Mark the log position so we only capture new entries
echo "[$(date +%H:%M:%S)] Starting log monitors..."

# Start tailing celery-worker logs for TIMING entries (background)
docker compose logs -f celery-worker 2>&1 | \
  grep --line-buffered -E "TIMING:|VRAM|perf_counter|TranscriptionPipeline|completed in|GPU memory|Reusing cached|Loading model|model loaded|Dispatched|Transcription completed" | \
  tee -a "$TIMING_FILE" &
TIMING_PID=$!

# Also capture full celery-worker logs for detailed analysis
docker compose logs -f celery-worker >> "$LOG_FILE" 2>&1 &
LOG_PID=$!

# Tail gpu-scaled worker too if it exists
docker compose logs -f celery-worker-gpu-scaled 2>&1 | \
  grep --line-buffered -E "TIMING:|VRAM|completed in|GPU memory|Reusing cached|Loading model|model loaded|Dispatched|Transcription completed" | \
  tee -a "$TIMING_FILE" &
TIMING_PID2=$!

docker compose logs -f celery-worker-gpu-scaled >> "$LOG_FILE" 2>&1 &
LOG_PID2=$!
# The followers run until killed. Only the completed/error paths killed them, so the
# 60-minute timeout fall-through, a `set -e` abort or Ctrl-C left four `logs -f` pipelines
# running for good (issue #1195). These are this script's own children: plain TERM.
stop_log_followers() {
  kill "$TIMING_PID" "$LOG_PID" "$TIMING_PID2" "$LOG_PID2" 2>/dev/null || true
}
trap stop_log_followers EXIT

# Give log monitors a moment to attach
sleep 1

# --- Step 4: Trigger reprocess ---
echo ""
echo "=============================================="
echo "[$(date +%H:%M:%S)] TRIGGERING REPROCESS (transcription only)..."
echo "=============================================="
WALL_START=$(date +%s.%N)
WALL_START_DISPLAY=$(date +%H:%M:%S)

REPROCESS_RESPONSE=$(curl -s -X POST "$API_BASE/files/$FILE_UUID/reprocess" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"stages": ["transcription"]}')

REPROCESS_STATUS=$(echo "$REPROCESS_RESPONSE" | jq -r '.status // empty')
if [ "$REPROCESS_STATUS" = "error" ] || [ -z "$REPROCESS_STATUS" ]; then
  echo "ERROR: Reprocess failed. Response:"
  echo "$REPROCESS_RESPONSE" | jq .
  kill $TIMING_PID $LOG_PID $TIMING_PID2 $LOG_PID2 2>/dev/null || true
  exit 1
fi
echo "[$(date +%H:%M:%S)] Reprocess triggered. Status: $REPROCESS_STATUS"
echo ""

# --- Step 5: Poll for completion ---
echo "[$(date +%H:%M:%S)] Waiting for transcription to complete..."
echo "  (Polling every 10 seconds, watching for status=completed)"
echo ""

POLL_COUNT=0
MAX_POLLS=360  # 60 minutes max wait
LAST_STATUS=""

while [ $POLL_COUNT -lt $MAX_POLLS ]; do
  sleep 10
  POLL_COUNT=$((POLL_COUNT + 1))

  STATUS_RESPONSE=$(curl -s -X GET "$API_BASE/files/$FILE_UUID" \
    -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo '{}')
  CURRENT_STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status // "unknown"')

  if [ "$CURRENT_STATUS" != "$LAST_STATUS" ]; then
    echo "  [$(date +%H:%M:%S)] Status changed: $LAST_STATUS -> $CURRENT_STATUS (poll #$POLL_COUNT)"
    LAST_STATUS="$CURRENT_STATUS"
  fi

  if [ "$CURRENT_STATUS" = "completed" ]; then
    WALL_END=$(date +%s.%N)
    WALL_ELAPSED=$(echo "$WALL_END - $WALL_START" | bc)
    WALL_MIN=$(echo "scale=1; $WALL_ELAPSED / 60" | bc)

    echo ""
    echo "=============================================="
    echo " BENCHMARK COMPLETE"
    echo "=============================================="
    echo ""
    echo "  File:            $FILENAME"
    echo "  Audio Duration:  ${DURATION_HR} hours (${DURATION_MIN} min)"
    echo "  File Size:       ${SIZE_MB} MB"
    echo ""
    echo "  Wall Clock Time: ${WALL_ELAPSED}s (${WALL_MIN} min)"
    echo "  Start:           $WALL_START_DISPLAY"
    echo "  End:             $(date +%H:%M:%S)"
    echo ""
    echo "  Realtime Factor: $(echo "scale=2; $DURATION / $WALL_ELAPSED" | bc 2>/dev/null || echo '?')x"
    echo "    (audio_duration / processing_time)"
    echo ""

    # Wait a moment for remaining log entries
    sleep 5

    # Extract and summarize TIMING entries
    echo "--- TIMING Breakdown ---"
    if [ -f "$TIMING_FILE" ]; then
      grep "TIMING:" "$TIMING_FILE" | sed 's/.*TIMING: /  /' | sort -t'=' -k2 -rn 2>/dev/null || \
      grep "TIMING:" "$TIMING_FILE" | sed 's/.*TIMING: /  /'
    else
      echo "  (no timing data captured)"
    fi
    echo ""

    echo "--- GPU/Model Events ---"
    if [ -f "$TIMING_FILE" ]; then
      grep -E "Reusing cached|Loading model|model loaded|GPU memory|VRAM" "$TIMING_FILE" | \
        sed 's/.*celery-worker[^ ]* | /  /' | head -30
    fi
    echo ""

    echo "Full logs:   $LOG_FILE"
    echo "Timing logs: $TIMING_FILE"
    echo ""

    # Cleanup
    kill $TIMING_PID $LOG_PID $TIMING_PID2 $LOG_PID2 2>/dev/null || true
    wait $TIMING_PID $LOG_PID $TIMING_PID2 $LOG_PID2 2>/dev/null || true
    exit 0
  fi

  if [ "$CURRENT_STATUS" = "error" ]; then
    WALL_END=$(date +%s.%N)
    WALL_ELAPSED=$(echo "$WALL_END - $WALL_START" | bc)
    echo ""
    echo "ERROR: Transcription failed after ${WALL_ELAPSED}s"
    echo "Status response:"
    echo "$STATUS_RESPONSE" | jq .
    echo ""
    echo "Last TIMING entries:"
    tail -20 "$TIMING_FILE" 2>/dev/null
    echo ""
    echo "Full logs: $LOG_FILE"

    kill $TIMING_PID $LOG_PID $TIMING_PID2 $LOG_PID2 2>/dev/null || true
    wait $TIMING_PID $LOG_PID $TIMING_PID2 $LOG_PID2 2>/dev/null || true
    exit 1
  fi

  # Print elapsed every 60 seconds
  if [ $((POLL_COUNT % 6)) -eq 0 ]; then
    NOW=$(date +%s.%N)
    ELAPSED=$(echo "$NOW - $WALL_START" | bc)
    ELAPSED_MIN=$(echo "scale=1; $ELAPSED / 60" | bc)
    echo "  [$(date +%H:%M:%S)] Still processing... (${ELAPSED_MIN} min elapsed, status=$CURRENT_STATUS)"
  fi
done

echo "ERROR: Timed out after $((MAX_POLLS * 10)) seconds"
kill $TIMING_PID $LOG_PID $TIMING_PID2 $LOG_PID2 2>/dev/null || true
exit 1
