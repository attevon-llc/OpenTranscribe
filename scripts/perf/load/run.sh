#!/bin/bash
# One load-test run into one directory: the stack sampler, Locust with a named profile, and the
# markdown summary with its pass/fail verdict.
#
#   scripts/perf/load/run.sh --fresh NAME --profile PROFILE [--mix MIX] [--gpus 1,2]
#                            [--out DIR] [-- extra locust arguments]
#
# PROFILE: smoke | steady | ramp | spike | soak | pipeline   (scripts/perf/load/profiles.py)
# MIX:     mixed (default: reads and writes, no recordings sent to the GPU)
#          read-only (no writes at all) | mixed-gpu (editors also upload real recordings)
#          The pipeline profile always uploads; --mix does not apply to it.
#
# Refuses anything but a ./opentr.sh --fresh stack (locustfile.py and sampler.py both check).
# Needs LOCUST pointing at the Locust venv's binary (README.md "Install"). Exit status: the
# summary's (0 pass, 1 a limit broken), or 2 when the run was refused or produced no data.
# Runbook: scripts/perf/load/README.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
LOCUST="${LOCUST:-locust}"

FRESH="" PROFILE="" MIX="mixed" GPUS="1" OUT=""
EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh)   FRESH="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --mix)     MIX="$2"; shift 2 ;;
        --gpus)    GPUS="$2"; shift 2 ;;
        --out)     OUT="$2"; shift 2 ;;
        --)        shift; EXTRA=("$@"); break ;;
        -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         echo "run.sh: unknown argument $1 (see --help)" >&2; exit 2 ;;
    esac
done

[[ -n "$FRESH" ]] || { echo "run.sh: --fresh NAME is required (an isolated stack only)" >&2; exit 2; }
[[ -f "${REPO_ROOT}/.fresh/${FRESH}.offset" ]] || {
    echo "run.sh: no --fresh deployment '${FRESH}' (.fresh/${FRESH}.offset is missing)" >&2; exit 2; }
case "$PROFILE" in
    smoke|steady|ramp|spike|soak|pipeline) ;;
    *) echo "run.sh: --profile must be smoke, steady, ramp, spike, soak or pipeline" >&2; exit 2 ;;
esac
TAGS=()
if [[ "$PROFILE" != pipeline ]]; then
    case "$MIX" in
        mixed)     TAGS=(--exclude-tags gpu) ;;
        read-only) TAGS=(--exclude-tags write gpu) ;;
        mixed-gpu) TAGS=() ;;
        *) echo "run.sh: --mix must be mixed, read-only or mixed-gpu" >&2; exit 2 ;;
    esac
fi
command -v "$LOCUST" >/dev/null || { echo "run.sh: set LOCUST to the Locust venv's locust" >&2; exit 2; }

RUN="${OUT:-${LOAD_RUNS_DIR:-$HOME/opentranscribe-load-runs}/$(date +%Y%m%d-%H%M%S)-${PROFILE}-${MIX}}"
mkdir -p "$RUN"
{
    echo "date:     $(date -Is)"
    echo "commit:   $(git -C "$REPO_ROOT" rev-parse HEAD) ($(git -C "$REPO_ROOT" status --short | wc -l) changed files)"
    echo "fresh:    ${FRESH} (offset $(tr -d '[:space:]' < "${REPO_ROOT}/.fresh/${FRESH}.offset"))"
    echo "profile:  ${PROFILE}   mix: ${MIX}   tags: ${TAGS[*]:-none}   gpus: ${GPUS}"
    echo "extra:    ${EXTRA[*]:-none}"
    echo "host:     $(uptime)"
    env | grep -E '^DEV_(RATE_LIMIT|MAX_CONCURRENT)' | sort || true
} > "${RUN}/env.txt"
echo "Run directory: ${RUN}"

python3 "${SCRIPT_DIR}/sampler.py" --fresh "$FRESH" --out "$RUN" --gpus "$GPUS" \
    > "${RUN}/sampler.log" 2>&1 &
SAMPLER_PID=$!
stop_sampler() {
    kill -TERM "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || true
}
trap stop_sampler EXIT

set +e
"$LOCUST" -f "${SCRIPT_DIR}/locustfile.py" --fresh "$FRESH" --load-profile "$PROFILE" --headless \
    --csv "${RUN}/locust" --csv-full-history --html "${RUN}/report.html" \
    "${TAGS[@]}" "${EXTRA[@]}" 2>&1 | tee "${RUN}/locust.log"
LOCUST_RC=${PIPESTATUS[0]}
set -e
stop_sampler
trap - EXIT

if [[ "$LOCUST_RC" -eq 2 ]]; then
    echo "run.sh: Locust refused the run (see ${RUN}/locust.log)" >&2
    exit 2
fi
python3 "${SCRIPT_DIR}/summarize.py" "$RUN"
