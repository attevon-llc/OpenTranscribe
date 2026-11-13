#!/bin/bash
# One load-test run into one directory: the stack sampler, Locust with a named profile, and the
# markdown summary with its pass/fail verdict.
#
#   scripts/perf/load/run.sh --fresh NAME --profile PROFILE [--mix MIX] [--gpus 1,2]
#                            [--out DIR] [-- extra locust arguments]
#
# PROFILE: smoke | steady | ramp | spike | soak | pipeline | knee | waves | herd | hot-file | egress | leak | soak-8h | soak-24h
#          (scripts/perf/load/profiles.py; knee, waves, egress and leak use the realistic people mix)
# MIX:     mixed (default: reads and writes, no recordings sent to the GPU)
#          read-only (no writes at all) | mixed-gpu (editors also upload real recordings)
#          The pipeline profile always uploads; --mix does not apply to it.
#
# Refuses anything but a ./opentr.sh --fresh stack. The stack is resolved and asserted by
# scripts/lib/stack-target.sh (schema at head, code=image, healthy, the raised rate limit in the
# backend's own environment); locustfile.py and sampler.py take its observed BACKEND_PORT.
# The stack must be `start dev --fresh NAME --no-bindmount --build` (README "Prepare the stack").
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
        -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         echo "run.sh: unknown argument $1 (see --help)" >&2; exit 2 ;;
    esac
done

[[ -n "$FRESH" ]] || { echo "run.sh: --fresh NAME is required (an isolated stack only)" >&2; exit 2; }
[[ -f "${REPO_ROOT}/.fresh/${FRESH}.offset" ]] || {
    echo "run.sh: no --fresh deployment '${FRESH}' (.fresh/${FRESH}.offset is missing)" >&2; exit 2; }
# shellcheck source=../../lib/stack-target.sh
source "${REPO_ROOT}/scripts/lib/stack-target.sh"
case "$PROFILE" in
    smoke|steady|ramp|spike|soak|pipeline|knee|waves|herd|hot-file|egress|leak|soak-8h|soak-24h) ;;
    *) echo "run.sh: --profile must be one of smoke steady ramp spike soak pipeline knee waves herd hot-file egress leak soak-8h soak-24h" >&2; exit 2 ;;
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

st_target --fresh "$FRESH" || exit $?
HEALTHY="healthy:postgres,redis,backend"
REQS=(schema-head code=image "$HEALTHY")
# egress measures the stack's default per-IP limits, so the raised one must not be demanded.
[[ "$PROFILE" == egress ]] || REQS+=(cenv:backend:RATE_LIMIT_AUTH_PER_MINUTE=100000)
if grep -qs 'docker-compose.monitoring.yml' "${REPO_ROOT}/.fresh/${FRESH}.aux"; then
    REQS+=(env:ALERTMANAGER_WEBHOOK_TOKEN)
fi
CHECKS_RC=0
CHECKS_OUT="$(st_require "${REQS[@]}" 2>&1)" || CHECKS_RC=$?
if [[ "$CHECKS_RC" -ne 0 ]]; then
    printf '%s\n' "$CHECKS_OUT" >&2
    exit "$CHECKS_RC"
fi

RUN="${OUT:-${LOAD_RUNS_DIR:-$HOME/opentranscribe-load-runs}/$(date +%Y%m%d-%H%M%S)-${PROFILE}-${MIX}}"
mkdir -p "$RUN"
{
    echo "date:     $(date -Is)"
    echo "commit:   $(git -C "$REPO_ROOT" rev-parse HEAD) ($(git -C "$REPO_ROOT" status --short | wc -l) changed files)"
    echo "fresh:    ${FRESH} (offset $(tr -d '[:space:]' < "${REPO_ROOT}/.fresh/${FRESH}.offset"))"
    echo "profile:  ${PROFILE}   mix: ${MIX}   tags: ${TAGS[*]:-none}   gpus: ${GPUS}"
    echo "extra:    ${EXTRA[*]:-none}"
    echo "host:     $(uptime)"
    echo "stack:    project=${ST_PROJECT} working_dir=${ST_WORKING_DIR} backend_port=${BACKEND_PORT}"
    echo "checks:   PASS ${REQS[*]}"
    [[ -z "$CHECKS_OUT" ]] || printf 'checks-note: %s\n' "$CHECKS_OUT"
} > "${RUN}/env.txt"
echo "Run directory: ${RUN}"

python3 "${SCRIPT_DIR}/sampler.py" --fresh "$FRESH" --out "$RUN" --gpus "$GPUS" \
    > "${RUN}/sampler.log" 2>&1 &
SAMPLER_PID=$!
SAMPLER_RC=0
stop_sampler() {
    kill -TERM "$SAMPLER_PID" 2>/dev/null || true
    wait "$SAMPLER_PID" 2>/dev/null || SAMPLER_RC=$?
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
# A sampler that died on its own (not our SIGTERM, 143) left a run with no stack data.
if [[ "$SAMPLER_RC" -ne 0 && "$SAMPLER_RC" -ne 143 ]]; then
    echo "run.sh: the sampler exited ${SAMPLER_RC} (see ${RUN}/sampler.log); the run has no stack data" >&2
    exit 2
fi

if [[ "$LOCUST_RC" -eq 2 ]]; then
    echo "run.sh: Locust refused the run (see ${RUN}/locust.log)" >&2
    exit 2
fi
python3 "${SCRIPT_DIR}/summarize.py" "$RUN"
