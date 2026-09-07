#!/usr/bin/env bash
#
# Run the @pytest.mark.gpu diarization suites on real GPU hardware (issue #577).
#
#   ./scripts/run-diarization-gpu-tests.sh                     # all three suites
#   ./scripts/run-diarization-gpu-tests.sh tests/integration/test_diarizer_lifecycle.py -v \
#        -o addopts= -m gpu                                    # your own pytest argv
#   DIARIZATION_PROBE_GPU=2 ./scripts/run-diarization-gpu-tests.sh
#
# WHY A SCRIPT: the three suites
#   backend/tests/integration/test_diarizer_lifecycle.py
#   backend/tests/integration/test_diarization_perf_gates.py
#   backend/tests/integration/test_diarization_regression.py
# only run under a specific set of conditions, and getting any one of them wrong
# produces a *green-looking* run that measured nothing:
#
#   * They need a GPU AND the production dependency stack — so they run in a container
#     built from Dockerfile.prod, not in backend/venv.
#   * They self-skip unless OPENTRANSCRIBE_IN_CONTAINER=1 or /.dockerenv exists, so a
#     host run reports "3 skipped" and looks fine.
#   * pyproject's addopts selector is `-m 'not integration and not gpu'`, so without
#     `-o addopts= -m gpu` pytest deselects all of them and exits 0. Since issue #719
#     `-o addopts= -m gpu` is ALSO what keeps CUDA devices visible at all: the fast
#     selector now hides every device (tests/conftest.py's pytest_configure), so argv
#     without `-m gpu` gets a `cuda-device-guard` pytest.fail, not a silent skip.
#   * BENCHMARK_ROOT (/app/benchmark/test_audio) is gitignored: with the fixtures
#     absent every test skips individually, again exiting 0. This script refuses to
#     run in that state rather than hand you a vacuous pass.
#
# The prod image ships no pytest, and installing it at run time with `--user root`
# splits site-packages and hides fastapi/meeteval — see backend/Dockerfile.test for the
# full explanation. This builds that additive test image instead.
#
# ⚠️ Isolated compose project. These runs use their own project name so they can never
# touch the dev stack's containers, and they build to `opentranscribe-backend-test`,
# never to `opentranscribe-backend:latest` (the tag every running worker uses).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.benchmark.yml"
COMPOSE_PROJECT="${DIARIZATION_TEST_PROJECT:-opentranscribe-diar-tests}"
BASE_IMAGE="${DIARIZATION_TEST_BASE_IMAGE:-opentranscribe-backend:latest}"
AUDIO_FIXTURE="${PROJECT_ROOT}/benchmark/test_audio/0.5h_1899s.wav"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
    echo -e "${RED}FAIL: base image '${BASE_IMAGE}' does not exist.${NC}"
    echo "      Build it with './opentr.sh start dev' (or './opentr.sh build'), or point"
    echo "      DIARIZATION_TEST_BASE_IMAGE at an image built from backend/Dockerfile.prod."
    exit 3
fi

if [[ ! -f "${AUDIO_FIXTURE}" ]]; then
    echo -e "${RED}FAIL: benchmark audio fixtures are missing.${NC}"
    echo "      Expected: ${AUDIO_FIXTURE}"
    echo "      benchmark/test_audio/ is gitignored (multi-GB WAVs). Without it every"
    echo "      test in these suites skips and the run exits 0 having measured nothing."
    echo "      See docs/diarization-vram-profile/README.md for how the fixtures are made."
    exit 3
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo -e "${YELLOW}WARN: nvidia-smi not found; the container may come up without a GPU,${NC}"
    echo -e "${YELLOW}      in which case every test skips on 'CUDA not available'.${NC}"
fi

echo -e "${YELLOW}Building ${BASE_IMAGE} + pytest -> opentranscribe-backend-test:latest${NC}"
docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" build diarization-tests

echo -e "${YELLOW}Running GPU diarization suites (card: ${DIARIZATION_PROBE_GPU:-\$GPU_DEVICE_ID})${NC}"
# `status=$?` on its own line would never be reached under `set -e`; capture with `||`.
status=0
if [[ $# -gt 0 ]]; then
    # Caller-supplied pytest argv. Nothing is added implicitly: if you want the gpu
    # tests selected you must pass `-o addopts= -m gpu` yourself, exactly as the
    # compose service's default command does.
    docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" \
        run --rm diarization-tests python -m pytest "$@" || status=$?
else
    # No args: the service's own `command:` in docker-compose.benchmark.yml is the
    # single source of truth for the default suite list and flags.
    docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" \
        run --rm diarization-tests || status=$?
fi

if [[ ${status} -eq 0 ]]; then
    echo -e "${GREEN}GPU diarization suites passed.${NC}"
else
    echo -e "${RED}GPU diarization suites FAILED (exit ${status}).${NC}"
fi
exit ${status}
