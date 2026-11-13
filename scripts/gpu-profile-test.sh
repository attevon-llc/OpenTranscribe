#!/usr/bin/env bash
# GPU VRAM Profiling Test Suite
#
# Reprocesses a spread of video durations to measure VRAM usage at each stage.
# Results are stored in Redis and accessible via GET /api/admin/gpu-profiles.
#
# Prerequisites:
#   - ENABLE_VRAM_PROFILING=true in .env
#   - Dev environment running: ./opentr.sh start dev --gpu-scale
#
# Usage:
#   ./scripts/gpu-profile-test.sh                    # Queue all 5 concurrently
#   ./scripts/gpu-profile-test.sh --solo             # Queue one at a time (wait between)
#   ./scripts/gpu-profile-test.sh --solo 1.0h_3758s  # Queue just one file
#   ./scripts/gpu-profile-test.sh --results          # Show results from last run
#   ./scripts/gpu-profile-test.sh --watch            # Tail GPU worker logs

set -euo pipefail

# shellcheck source=scripts/lib/stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/stack-target.sh"
if [[ -z "${API_URL:-}" ]]; then st_target || exit $?; fi
API_URL="${API_URL:-http://localhost:${BACKEND_PORT}}"

# Reference test files (from database, sorted by duration)
# These are stable UUIDs for completed files at various durations.
declare -A TEST_FILES=(
  ["4.7h_17044s"]="3e313bbd-924f-4a4b-9584-fa24532b9a01"  # Protect Our Parks 6
  ["3.2h_11495s"]="d734bb4b-0296-4e05-8122-8228e2cea1d5"  # Jimmy Carr
  ["2.2h_7998s"]="8cf209c3-6fc5-4c03-b867-d37e2fe33ac6"   # Jordan Jonas
  ["1.0h_3758s"]="b6375779-1675-4752-ab43-de246664d419"    # Dom Irrera
  ["0.5h_1899s"]="0ba0d6ed-bcca-4be6-9176-0b1a05904fab"   # Chris Aubrey Marcus
)

# Sorted order for display
DURATIONS=("4.7h_17044s" "3.2h_11495s" "2.2h_7998s" "1.0h_3758s" "0.5h_1899s")

get_token() {
  curl -s -X POST "$API_URL/api/auth/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=admin@example.com&password=password" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}

show_results() {
  local token
  token=$(get_token)
  echo "=== GPU VRAM Profile Results ==="
  echo ""
  curl -s "$API_URL/api/admin/gpu-profiles?limit=50" \
    -H "Authorization: Bearer $token" | \
    python3 -c "
import sys, json
profiles = json.load(sys.stdin)
if not profiles:
    print('No profiles found. Run the test first or wait for processing to complete.')
    sys.exit(0)

print(f'  {\"Duration\":>12s} | {\"Spk\":>3s} | {\"Device Peak\":>11s} | {\"Whisper Δ\":>9s} | {\"PyAnnote Δ\":>10s} | {\"Transcribe\":>10s} | {\"Diarize\":>8s} | {\"Total\":>7s} | Task')
print('  ' + '-'*110)

for p in profiles:
    tid = p.get('task_id', '?')[:12]
    dur = p.get('audio_duration_s', 0)
    spk = p.get('num_speakers', 0)
    total = p.get('total_duration_s', 0)

    steps = p.get('steps', [])

    # NVML device_used deltas (captures CTranslate2 + PyTorch + everything)
    start_used = next((s['device_used_after_mb'] for s in steps if s['name'] == 'snapshot:pipeline_start'), 0)
    after_whisper = next((s['device_used_after_mb'] for s in steps if s['name'] == 'snapshot:after_transcriber_loaded'), 0)
    after_diarize = next((s['device_used_after_mb'] for s in steps if s['name'] == 'snapshot:after_diarization'), 0)
    peak_device = p.get('peak_device_used_mb', 0)

    # Fall back to old format (device_free based) if new fields missing
    if not start_used:
        start_free = next((s.get('device_free_after_mb',0) for s in steps if s['name'] == 'snapshot:pipeline_start'), 0)
        whisper_free = next((s.get('device_free_after_mb',0) for s in steps if s['name'] == 'snapshot:after_transcriber_loaded'), 0)
        diarize_free = next((s.get('device_free_after_mb',0) for s in steps if s['name'] == 'snapshot:after_diarization'), 0)
        whisper_delta = (start_free - whisper_free) if start_free and whisper_free else 0
        pyannote_delta = (whisper_free - diarize_free) if whisper_free and diarize_free else 0
    else:
        whisper_delta = after_whisper - start_used if after_whisper else 0
        pyannote_delta = after_diarize - after_whisper if after_diarize and after_whisper else 0

    transcribe_t = next((s['duration_s'] for s in steps if s['name'] == 'transcription'), 0)
    diarize_t = next((s['duration_s'] for s in steps if s['name'] == 'diarization'), 0)

    dur_str = f'{dur/3600:.1f}h ({dur:.0f}s)'
    print(f'  {dur_str:>12s} | {spk:>3d} | {peak_device:>8.0f} MB | {whisper_delta:>+7.0f} MB | {pyannote_delta:>+8.0f} MB | {transcribe_t:>8.1f} s | {diarize_t:>6.1f} s | {total:>5.1f} s | {tid}')
print()
print('  Whisper Δ  = NVML device memory change from model load (includes CTranslate2)')
print('  PyAnnote Δ = NVML device memory change from diarization (includes inference buffers)')
print('  Device Peak = maximum NVML device_used across all steps')
"
}

watch_logs() {
  echo "=== Tailing GPU worker logs (Ctrl+C to stop) ==="
  [[ -n "${ST_PROJECT:-}" ]] || st_target || exit $?
  docker logs -f "$(st_container celery-worker-gpu-scaled)" 2>&1 | \
    grep --line-buffered -E "VRAM_PROFILE"
}

reprocess_file() {
  local token="$1" uuid="$2"
  curl -s -X POST "$API_URL/api/files/$uuid/reprocess" \
    -H "Authorization: Bearer $token" | \
    python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('status','error'), d.get('filename','')[:50], d.get('detail',''))
" 2>/dev/null || echo "failed"
}

wait_for_file() {
  local token="$1" uuid="$2"
  echo -n "    Waiting for completion"
  while true; do
    status=$(curl -s "$API_URL/api/files/$uuid" -H "Authorization: Bearer $token" | \
      python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null)
    case "$status" in
      completed|error) echo " → $status"; return ;;
    esac
    echo -n "."
    sleep 15
  done
}

run_concurrent() {
  local token
  token=$(get_token)

  echo "=== GPU VRAM Profiling: CONCURRENT (all 5 at once) ==="
  echo "All files will run simultaneously to measure shared VRAM under load."
  echo ""

  for dur in "${DURATIONS[@]}"; do
    uuid="${TEST_FILES[$dur]}"
    echo -n "  $dur | "
    reprocess_file "$token" "$uuid"
  done

  echo ""
  echo "Files queued. Monitor with:"
  echo "  ./scripts/gpu-profile-test.sh --watch"
  echo "  watch -n1 nvidia-smi"
  echo ""
  echo "View results when done:"
  echo "  ./scripts/gpu-profile-test.sh --results"
}

run_solo() {
  local token filter_dur="${1:-}"
  token=$(get_token)

  echo "=== GPU VRAM Profiling: SOLO (one at a time) ==="
  echo "Each file runs alone for accurate per-file VRAM measurement."
  echo ""

  for dur in "${DURATIONS[@]}"; do
    if [[ -n "$filter_dur" && "$dur" != "$filter_dur" ]]; then
      continue
    fi

    uuid="${TEST_FILES[$dur]}"
    echo "  [$dur] $uuid"
    echo -n "    Submitting: "
    reprocess_file "$token" "$uuid"

    # Re-fetch token in case it expires during long waits
    token=$(get_token)
    wait_for_file "$token" "$uuid"
    echo ""
  done

  echo "All done. View results:"
  echo "  ./scripts/gpu-profile-test.sh --results"
}

case "${1:-}" in
  --results) show_results ;;
  --watch)   watch_logs ;;
  --solo)    run_solo "${2:-}" ;;
  *)         run_concurrent ;;
esac
