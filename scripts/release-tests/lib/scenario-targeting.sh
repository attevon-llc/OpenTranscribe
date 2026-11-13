#!/bin/bash
# Sourced by guardrails.sh. How a release-test scenario finds ITS OWN stack and proves the images it
# runs are this checkout's (STACK_TARGETING_PLAN P4, R6-R8). Needs REPO_ROOT and TEST_SCENARIO.

# The compose project the scenario's stack runs under: lite pins its own `ot-reltest-lite`; fresh
# install and upgrade use the installer's stock `opentranscribe` (TEST_PROJECT_NAME is only a label
# namespace there, not a compose project).
if [[ "${TEST_SCENARIO:-}" == "lite-mode" ]]; then
    GR_SCENARIO_PROJECT="${GR_SCENARIO_PROJECT:-${TEST_PROJECT_NAME:?}}"
else
    GR_SCENARIO_PROJECT="${GR_SCENARIO_PROJECT:-opentranscribe}"
fi
export GR_SCENARIO_PROJECT
# Stamped on every image a scenario builds locally so gr_assert_image_is_the_code_under_test can attribute it.
GR_REVISION_LABEL="org.opencontainers.image.revision=$(git -C "${REPO_ROOT:-.}" rev-parse HEAD 2>/dev/null || echo unknown)"
export GR_REVISION_LABEL

# gr_assert_release_images_are_the_code_under_test TAG SUFFIX...
#   One gr_assert_image_is_the_code_under_test per image the scenario runs, for HEAD. A scenario
#   that checked only the backend rehearsed a stale frontend or docs image as "this release"
#   (STACK_TARGETING_PLAN R6-R8). SUFFIX is the repo's image name after `opentranscribe-`
#   (backend, backend-lite, frontend, docs). Call before `up`.
gr_assert_release_images_are_the_code_under_test() {
    local tag="$1" suffix head; shift
    head="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
    for suffix in "$@"; do
        gr_assert_image_is_the_code_under_test "davidamacey/opentranscribe-${suffix}:${tag}" "$head"
    done
}

# gr_service_container SERVICE
#   The running container of SERVICE in the scenario's own compose project (label match, never a
#   `opentranscribe-<svc>` name: that name is the stock stack's whichever project owns it).
#   Prints "" when none runs.
gr_service_container() {
    local out rc=0
    out="$(python3 "${REPO_ROOT:?}/scripts/lib/stack_target.py" container "${1:?service}" \
        --project "${GR_SCENARIO_PROJECT:?GR_SCENARIO_PROJECT unset}" --no-banner 2>/dev/null)" || rc=$?
    (( rc == 0 )) && printf '%s\n' "$out"
    return 0
}
