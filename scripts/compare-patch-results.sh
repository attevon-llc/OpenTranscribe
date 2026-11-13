#!/usr/bin/env bash
# Compare diarization results before and after patches
#
# Captures speaker segments, timestamps, and speaker assignments from the API,
# then compares to verify patches don't change results.
#
# Usage:
#   ./scripts/compare-patch-results.sh capture baseline    # Save current results as "baseline"
#   ./scripts/compare-patch-results.sh capture patched     # Save current results as "patched"
#   ./scripts/compare-patch-results.sh diff baseline patched  # Compare two captures
#   ./scripts/compare-patch-results.sh list                # Show saved captures

set -euo pipefail

# shellcheck source=scripts/lib/stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/stack-target.sh"
if [[ -z "${API_URL:-}" ]]; then st_target || exit $?; fi
API_URL="${API_URL:-http://localhost:${BACKEND_PORT}}"
RESULTS_DIR="${RESULTS_DIR:-/mnt/nvm/repos/transcribe-app/test-results/patch-comparison}"

# Same test files as gpu-profile-test.sh
declare -A TEST_FILES=(
  ["4.7h_17044s"]="3e313bbd-924f-4a4b-9584-fa24532b9a01"
  ["3.2h_11495s"]="d734bb4b-0296-4e05-8122-8228e2cea1d5"
  ["2.2h_7998s"]="8cf209c3-6fc5-4c03-b867-d37e2fe33ac6"
  ["1.0h_3758s"]="b6375779-1675-4752-ab43-de246664d419"
  ["0.5h_1899s"]="0ba0d6ed-bcca-4be6-9176-0b1a05904fab"
)

DURATIONS=("4.7h_17044s" "3.2h_11495s" "2.2h_7998s" "1.0h_3758s" "0.5h_1899s")

get_token() {
  curl -s -X POST "$API_URL/api/auth/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=admin@example.com&password=password" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
}

capture_results() {
  local label="$1"
  local capture_dir="$RESULTS_DIR/$label"
  mkdir -p "$capture_dir"

  local token
  token=$(get_token)

  echo "=== Capturing results as '$label' ==="
  echo "  Output: $capture_dir"
  echo ""

  for dur in "${DURATIONS[@]}"; do
    uuid="${TEST_FILES[$dur]}"
    echo -n "  $dur ($uuid): "

    # Get file metadata (includes speaker count, status)
    curl -s "$API_URL/api/files/$uuid" \
      -H "Authorization: Bearer $token" > "$capture_dir/${dur}_meta.json" 2>/dev/null

    # Extract key metrics from file response
    python3 -c "
import json, sys

meta_path = sys.argv[1]
summary_path = sys.argv[2]
segments_path = sys.argv[3]

with open(meta_path) as f:
    meta = json.load(f)

status = meta.get('status', 'unknown')
speakers = meta.get('speakers', [])
num_speakers = len(speakers)
total_segs = meta.get('total_segments', 0)
all_segments = meta.get('transcript_segments', [])

def get_speaker_name(seg):
    spk = seg.get('speaker', '')
    if isinstance(spk, dict):
        return spk.get('name', '')
    return str(spk)

comparison = [
    {
        'start': round(s['start_time'], 3),
        'end': round(s['end_time'], 3),
        'speaker': get_speaker_name(s),
        'text': s.get('text', '')[:100],
    }
    for s in all_segments
]

speaker_names = sorted(set(c['speaker'] for c in comparison))
print(f'{status} | {num_speakers} speakers | {total_segs} total segs | {len(comparison)} captured')

summary = {
    'status': status,
    'num_speakers': num_speakers,
    'num_segments': total_segs,
    'segment_count_captured': len(comparison),
    'speaker_labels': speaker_names,
}
with open(summary_path, 'w') as f:
    json.dump(summary, f, indent=2)

with open(segments_path, 'w') as f:
    json.dump(comparison, f, indent=2)
" "$capture_dir/${dur}_meta.json" "$capture_dir/${dur}_summary.json" "$capture_dir/${dur}_segments.json" || echo "failed"

  done

  echo ""
  echo "  Capture complete: $capture_dir"

  # Also capture GPU profiles
  echo ""
  echo "  Capturing GPU profiles..."
  curl -s "$API_URL/api/admin/gpu-profiles?limit=50" \
    -H "Authorization: Bearer $token" > "$capture_dir/gpu_profiles.json" 2>/dev/null
  echo "  Done"
}

diff_results() {
  local label_a="$1"
  local label_b="$2"
  local dir_a="$RESULTS_DIR/$label_a"
  local dir_b="$RESULTS_DIR/$label_b"

  if [[ ! -d "$dir_a" ]]; then
    echo "ERROR: Capture '$label_a' not found at $dir_a"
    exit 1
  fi
  if [[ ! -d "$dir_b" ]]; then
    echo "ERROR: Capture '$label_b' not found at $dir_b"
    exit 1
  fi

  echo "=== Comparing '$label_a' vs '$label_b' ==="
  echo ""

  python3 -c "
import json, os, sys

dir_a = '$dir_a'
dir_b = '$dir_b'
durations = ['4.7h_17044s', '3.2h_11495s', '2.2h_7998s', '1.0h_3758s', '0.5h_1899s']

print(f'  {\"Duration\":>12s} | {\"Metric\":>12s} | {\"$label_a\":>10s} | {\"$label_b\":>10s} | Match')
print('  ' + '-'*65)

all_match = True

for dur in durations:
    sum_a_path = f'{dir_a}/{dur}_summary.json'
    sum_b_path = f'{dir_b}/{dur}_summary.json'

    if not os.path.exists(sum_a_path) or not os.path.exists(sum_b_path):
        print(f'  {dur:>12s} | {\"MISSING\":>12s} | --- | --- | ---')
        continue

    with open(sum_a_path) as f:
        sa = json.load(f)
    with open(sum_b_path) as f:
        sb = json.load(f)

    for metric in ['num_speakers', 'num_segments', 'num_words']:
        va = sa.get(metric, '?')
        vb = sb.get(metric, '?')
        match = 'YES' if va == vb else 'NO'
        if va != vb:
            all_match = False
        print(f'  {dur:>12s} | {metric:>12s} | {str(va):>10s} | {str(vb):>10s} | {match}')

    # Compare speaker assignments from segments
    trans_a_path = f'{dir_a}/{dur}_segments.json'
    trans_b_path = f'{dir_b}/{dur}_segments.json'
    if os.path.exists(trans_a_path) and os.path.exists(trans_b_path):
        with open(trans_a_path) as f:
            segs_a = json.load(f)
        with open(trans_b_path) as f:
            segs_b = json.load(f)

        speakers_a = [s.get('speaker', '') for s in segs_a]
        speakers_b = [s.get('speaker', '') for s in segs_b]

        if len(speakers_a) == len(speakers_b):
            mismatches = sum(1 for a, b in zip(speakers_a, speakers_b) if a != b)
            pct = (1 - mismatches / max(len(speakers_a), 1)) * 100
            match = 'YES' if pct >= 99.0 else f'{pct:.1f}%'
            if pct < 99.0:
                all_match = False
            print(f'  {dur:>12s} | {\"spk_assign\":>12s} | {len(speakers_a):>10d} | {len(speakers_b):>10d} | {match}')
        else:
            all_match = False
            print(f'  {dur:>12s} | {\"spk_assign\":>12s} | {len(speakers_a):>10d} | {len(speakers_b):>10d} | DIFF_LEN')

    print()

# GPU profile comparison
print('  === GPU Profile Comparison ===')
print()

for label, d in [('$label_a', dir_a), ('$label_b', dir_b)]:
    gp_path = f'{d}/gpu_profiles.json'
    if os.path.exists(gp_path):
        with open(gp_path) as f:
            profiles = json.load(f)
        print(f'  [{label}]')
        for p in profiles[:5]:
            dur_s = p.get('audio_duration_s', 0)
            peak = p.get('peak_device_used_mb', 0)
            total = p.get('total_duration_s', 0)
            spk = p.get('num_speakers', 0)
            print(f'    {dur_s/3600:.1f}h | peak={peak:.0f}MB | total={total:.1f}s | {spk} speakers')
        print()

if all_match:
    print('  RESULT: All metrics match — patch produces identical results')
else:
    print('  RESULT: Some metrics differ — investigate before submitting PR')
"
}

list_captures() {
  echo "=== Saved captures ==="
  if [[ ! -d "$RESULTS_DIR" ]]; then
    echo "  No captures found at $RESULTS_DIR"
    return
  fi
  for d in "$RESULTS_DIR"/*/; do
    if [[ -d "$d" ]]; then
      label=$(basename "$d")
      count=$(find "$d" -name "*_summary.json" | wc -l)
      echo "  $label  ($count files captured)"
    fi
  done
}

case "${1:-}" in
  capture) capture_results "${2:?Usage: $0 capture <label>}" ;;
  diff)    diff_results "${2:?Usage: $0 diff <label_a> <label_b>}" "${3:?}" ;;
  list)    list_captures ;;
  *)
    echo "Usage:"
    echo "  $0 capture <label>         # Save current results"
    echo "  $0 diff <label_a> <label_b>  # Compare two captures"
    echo "  $0 list                    # Show saved captures"
    ;;
esac
