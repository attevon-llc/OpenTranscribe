#!/bin/bash
# Regression test for issue #681 — "a component that was never scanned must never
# read as a component that was scanned and found clean".
#
# The defect: scripts/docker-build-push.sh's run_security_scan collapsed TWO
# different non-zero results from scripts/security-scan.sh into one branch —
# "scanned, found tolerable CVEs" and "could not scan at all". With
# FAIL_ON_SECURITY_ISSUES defaulting to false, an unscannable component returned
# 0, its status file was written as 0, and the run ended with
# "All security scans completed successfully!". `docs` was already in that state:
# it was in BUILT_COMPONENTS and had a security-scan.sh arm, but the registry-pull
# dispatch handled only backend and frontend, so the default path scanned it
# against an image it never fetched.
#
# WHY THIS TEST IS SHAPED LIKE THIS
#
# None of these paths can be reached through a real build: that needs Docker Hub
# credentials and multi-gigabyte images, so in practice they would not be tested
# at all. Instead the test SOURCES docker-build-push.sh (which is why that script
# now guards its `main` call with a BASH_SOURCE check) and drives the three
# functions that own the decision directly:
#
#   run_security_scan       — does it distinguish the two failures?
#   evaluate_scan_statuses  — does the collector distinguish them?
#   run_parallel_scans      — can the SUMMARY still claim success?
#
# Two of the cases invoke the real security-scan.sh with a real unknown component
# name; the rest use a throwaway scanner stub with a chosen exit code, so the
# caller's branching is tested independently of the scanner's.
#
# Every case is either a MUST-FIRE (the defect must make it fail) or a
# MUST-STAY-CLEAN (correct behaviour must not be broken by the fix). A gate that
# only ever fails is as useless as one that only ever passes.
#
# No Docker, no network, no images, no built artifacts.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_SCRIPT="$REPO_ROOT/scripts/docker-build-push.sh"

# Every repo file this suite makes an assertion ABOUT — the same declared contract
# scripts/tests/test-publish-platforms.sh carries, read by
# backend/tests/unit/test_precommit_hook_file_scope.py, which fails if this hook's `files:`
# pattern in .pre-commit-config.yaml does not select every entry (or if this array omits a
# path the script reads). This suite's pattern was already correct when the check was added;
# the declaration exists so it CANNOT silently stop being correct, which is what happened to
# the sibling hook.
SUBJECT_FILES=(
    scripts/docker-build-push.sh
    scripts/security-scan.sh
    scripts/tests/test-scan-not-a-pass.sh
)
# Must stay ABOVE every echo in this file: the contract is that this mode prints paths and
# nothing else.
[ "${OT_PRINT_SUBJECT_FILES:-}" = "1" ] && { printf '%s\n' "${SUBJECT_FILES[@]}"; exit 0; }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

REPORTS_DIR="$TMP_ROOT/reports"
DOCKER_LOG="$TMP_ROOT/docker.log"
: > "$DOCKER_LOG"

pass=0
fail=0
ok() { echo "  ok   - $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL - $1"; fail=$((fail + 1)); }

# Run a case function with docker-build-push.sh sourced, in an isolated subshell.
#
# Two stubs, both for things that are NOT under test here and that would
# otherwise reach the outside world: the vulnerability-database refresh (a
# multi-minute network download when trivy/grype are installed) and `docker`
# itself, which is replaced by a logger so the pull dispatch can be asserted on
# without pulling anything. `docker` is a shell function, so it is deliberately
# invisible to security-scan.sh, which runs as a child process.
lib_run() {
    local fn="$1"
    (
        set +u  # neither script under test runs with `set -u`
        cd "$REPO_ROOT" || exit 99
        # shellcheck source=/dev/null
        . "$BUILD_SCRIPT"
        update_security_tools() { print_info "[stub] vulnerability DB update skipped"; }
        docker() { echo "docker $*" >> "$DOCKER_LOG"; return 0; }
        "$fn"
    )
}

# Write a throwaway scripts/security-scan.sh that exits with a chosen code, and
# echo the sandbox directory. run_security_scan resolves the scanner relative to
# cwd, so a case can cd here to control exactly what the scanner reports.
make_scanner_sandbox() {
    local name="$1" exit_code="$2"
    local sandbox="$TMP_ROOT/$name"
    rm -rf "$sandbox"
    mkdir -p "$sandbox/scripts"
    printf '#!/bin/bash\nexit %s\n' "$exit_code" > "$sandbox/scripts/security-scan.sh"
    chmod +x "$sandbox/scripts/security-scan.sh"
    echo "$sandbox"
}

echo "== security-scan.sh: exit codes distinguish 'found something' from 'never looked' =="

# MUST-FIRE. Before the fix this exited 1 — the same code as "scanned, has
# findings" — which is what made the two indistinguishable to every caller.
rc=0
( cd "$REPO_ROOT" && OUTPUT_DIR="$REPORTS_DIR" ./scripts/security-scan.sh bogus ) \
    > "$TMP_ROOT/scan-bogus.out" 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then
    ok "an unknown component exits 2 (could not scan), not 1 (findings)"
else
    bad "an unknown component exited $rc; expected 2 (could not scan)"
fi

# MUST-STAY-CLEAN: the machine-readable component contract other scripts derive
# their lists from must actually list the published images (issue #680 added
# `lite` and `blackwell` alongside the original three).
known="$( (cd "$REPO_ROOT" && OUTPUT_DIR="$REPORTS_DIR" ./scripts/security-scan.sh list-components 2>/dev/null) | tr '\n' ' ')"
if [ "$known" = "backend blackwell docs frontend lite " ]; then
    ok "list-components reports the published components: ${known% }"
else
    bad "list-components reported '${known:0:120}'; expected 'backend blackwell docs frontend lite '"
fi

echo "== run_security_scan: the policy flag tolerates findings, never the absence of a scan =="

# MUST-FIRE. This is the defect itself: FAIL_ON_SECURITY_ISSUES=false (the
# default) used to turn "this component cannot be scanned" into a return of 0.
case_unscannable_component_is_fatal() {
    export FAIL_ON_SECURITY_ISSUES=false
    export SKIP_SECURITY_SCAN=false
    local rc=0
    OUTPUT_DIR="$REPORTS_DIR" run_security_scan bogus > "$TMP_ROOT/rss-bogus.out" 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_unscannable_component_is_fatal || rc=$?
if [ "$rc" -eq 2 ]; then
    ok "an unscannable component is fatal even with FAIL_ON_SECURITY_ISSUES=false (rc=$rc)"
else
    bad "an unscannable component returned $rc with FAIL_ON_SECURITY_ISSUES=false; expected 2"
fi

# MUST-FIRE, scanner-independent: a scanner exit of 2 must not be readable as a
# finding regardless of what the real security-scan.sh does.
case_scanner_exit_2_is_fatal() {
    local sandbox
    sandbox="$(make_scanner_sandbox sandbox-cannot-scan 2)"
    cd "$sandbox" || return 99
    export FAIL_ON_SECURITY_ISSUES=false
    export SKIP_SECURITY_SCAN=false
    local rc=0
    run_security_scan backend > /dev/null 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_scanner_exit_2_is_fatal || rc=$?
if [ "$rc" -eq 2 ]; then
    ok "a scanner exit of 2 stays fatal under FAIL_ON_SECURITY_ISSUES=false (rc=$rc)"
else
    bad "a scanner exit of 2 returned $rc under FAIL_ON_SECURITY_ISSUES=false; expected 2"
fi

# MUST-STAY-CLEAN: tolerating FINDINGS is a legitimate policy choice and the fix
# must not have taken it away.
case_findings_still_tolerated() {
    local sandbox
    sandbox="$(make_scanner_sandbox sandbox-findings 1)"
    cd "$sandbox" || return 99
    export FAIL_ON_SECURITY_ISSUES=false
    export SKIP_SECURITY_SCAN=false
    local rc=0
    run_security_scan backend > /dev/null 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_findings_still_tolerated || rc=$?
if [ "$rc" -eq 0 ]; then
    ok "findings are still tolerated when FAIL_ON_SECURITY_ISSUES=false"
else
    bad "findings returned $rc under FAIL_ON_SECURITY_ISSUES=false; expected 0"
fi

# MUST-STAY-CLEAN: ...and still refused when the operator asks for that.
case_findings_refused_when_asked() {
    local sandbox
    sandbox="$(make_scanner_sandbox sandbox-findings-strict 1)"
    cd "$sandbox" || return 99
    export FAIL_ON_SECURITY_ISSUES=true
    export SKIP_SECURITY_SCAN=false
    local rc=0
    run_security_scan backend > /dev/null 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_findings_refused_when_asked || rc=$?
if [ "$rc" -eq 1 ]; then
    ok "findings are refused when FAIL_ON_SECURITY_ISSUES=true"
else
    bad "findings returned $rc under FAIL_ON_SECURITY_ISSUES=true; expected 1"
fi

echo "== evaluate_scan_statuses: three outcomes, not two =="

make_status_dir() {
    local name="$1"
    local dir="$TMP_ROOT/$name"
    rm -rf "$dir"
    mkdir -p "$dir"
    echo "$dir"
}

# MUST-STAY-CLEAN
case_eval_all_clean() {
    local d
    d="$(make_status_dir st-clean)"
    echo 0 > "$d/backend.status"
    echo 0 > "$d/docs.status"
    local rc=0
    evaluate_scan_statuses "$d" backend docs > /dev/null 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_eval_all_clean || rc=$?
if [ "$rc" -eq 0 ]; then
    ok "all-clean statuses evaluate to 0"
else
    bad "all-clean statuses evaluated to $rc; expected 0"
fi

# MUST-FIRE
case_eval_findings() {
    local d
    d="$(make_status_dir st-findings)"
    echo 0 > "$d/backend.status"
    echo 1 > "$d/docs.status"
    local rc=0
    evaluate_scan_statuses "$d" backend docs > /dev/null 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_eval_findings || rc=$?
if [ "$rc" -eq 1 ]; then
    ok "a findings status evaluates to 1 (scanned, has findings)"
else
    bad "a findings status evaluated to $rc; expected 1"
fi

# MUST-FIRE — the missing-file arm. This is the case that costs nothing to hit
# in production (a subshell killed before it writes) and that previously left
# all_passed untouched, i.e. scored as a pass.
case_eval_missing_status() {
    local d
    d="$(make_status_dir st-missing)"
    echo 0 > "$d/backend.status"
    # docs.status deliberately absent
    local rc=0
    evaluate_scan_statuses "$d" backend docs > /dev/null 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_eval_missing_status || rc=$?
if [ "$rc" -eq 2 ]; then
    ok "a MISSING status file evaluates to 'not scanned' (rc=$rc)"
else
    bad "a missing status file evaluated to $rc; expected 2"
fi

# MUST-FIRE
case_eval_could_not_scan_status() {
    local d
    d="$(make_status_dir st-cannot)"
    echo 0 > "$d/backend.status"
    echo 2 > "$d/docs.status"
    local rc=0
    evaluate_scan_statuses "$d" backend docs > /dev/null 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_eval_could_not_scan_status || rc=$?
if [ "$rc" -eq 2 ]; then
    ok "a could-not-scan status evaluates to 'not scanned' (rc=$rc)"
else
    bad "a could-not-scan status evaluated to $rc; expected 2"
fi

echo "== run_parallel_scans: the summary cannot claim success for an unscanned component =="

SUCCESS_BANNER="All security scans completed successfully!"

# MUST-FIRE. The headline symptom of #681, end to end.
case_parallel_unscannable_component() {
    export SKIP_SECURITY_SCAN=false
    export FAIL_ON_SECURITY_ISSUES=false
    local rc=0
    run_parallel_scans bogus > "$TMP_ROOT/parallel-bogus.out" 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_parallel_unscannable_component || rc=$?
if [ "$rc" -ne 0 ]; then
    ok "run_parallel_scans fails for a component that cannot be scanned (rc=$rc)"
else
    bad "run_parallel_scans returned 0 for a component that cannot be scanned"
fi
if grep -qF "$SUCCESS_BANNER" "$TMP_ROOT/parallel-bogus.out"; then
    bad "the summary printed '$SUCCESS_BANNER' for a component that was never scanned"
else
    ok "the summary does NOT claim success for a component that was never scanned"
fi

# MUST-FIRE — the second, independent gap in the same function: the registry-pull
# dispatch handled only backend and frontend, so docs was scanned against an
# image that was never fetched.
case_docs_is_pulled_from_the_registry() {
    : > "$DOCKER_LOG"
    export SKIP_SECURITY_SCAN=false
    run_security_scan() { return 0; }  # not what this case is about
    local rc=0
    run_parallel_scans docs > "$TMP_ROOT/parallel-docs.out" 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_docs_is_pulled_from_the_registry || rc=$?
if grep -q 'pull .*opentranscribe-docs:latest' "$DOCKER_LOG"; then
    ok "the docs image is pulled from the registry before being scanned"
else
    bad "the docs image was never pulled — it would be scanned against a stale or absent local image"
fi

# MUST-FIRE — defence in depth: even if the up-front component validation is
# bypassed, the pull dispatch itself must refuse an unknown component rather
# than silently skipping the pull.
case_pull_dispatch_refuses_unknown() {
    : > "$DOCKER_LOG"
    export SKIP_SECURITY_SCAN=false
    assert_components_scannable() { return 0; }  # bypass the earlier guard
    run_security_scan() { return 0; }
    local rc=0
    run_parallel_scans bogus > "$TMP_ROOT/parallel-nopull.out" 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_pull_dispatch_refuses_unknown || rc=$?
if [ "$rc" -ne 0 ] && grep -q 'No registry-pull rule' "$TMP_ROOT/parallel-nopull.out"; then
    ok "the registry-pull dispatch refuses a component it has no rule for"
else
    bad "the pull dispatch silently skipped an unknown component (rc=$rc)"
fi

# MUST-STAY-CLEAN: a genuinely clean run still reports success. Without this the
# suite could be satisfied by a summary that never says anything good.
case_parallel_all_clean_reports_success() {
    : > "$DOCKER_LOG"
    export SKIP_SECURITY_SCAN=false
    run_security_scan() { return 0; }
    local rc=0
    run_parallel_scans backend frontend docs > "$TMP_ROOT/parallel-clean.out" 2>&1 || rc=$?
    return "$rc"
}
rc=0
lib_run case_parallel_all_clean_reports_success || rc=$?
if [ "$rc" -eq 0 ] && grep -qF "$SUCCESS_BANNER" "$TMP_ROOT/parallel-clean.out"; then
    ok "a genuinely clean run still reports success"
else
    bad "a clean run did not report success (rc=$rc)"
fi

echo "== security-scan.sh 'all': one unscannable component poisons the whole verdict =="

# The `all` target is what the release scan gate (scripts/release/50-scan.sh)
# runs. Its aggregation used to be `backend_exit + frontend_exit + docs_exit` —
# a SUM, which cannot express three outcomes, and which the old flat
# `exit 0 / exit 1` ending then flattened again. Measured against the
# pre-fix code: the unscannable case below exits 1, indistinguishable from
# findings. The clean and findings cases already behaved correctly and are here
# as regression guards on the restructure, not as evidence of the bug.
# All of them use stubbed scanners; doing it for real needs the three published
# multi-gigabyte images.
#
# $1 = per-component exit code, as "component:code" pairs; anything unlisted is 0.
scan_all_with() {
    local spec="$1" out="$2"
    (
        set +u
        cd "$REPO_ROOT" || exit 99
        export OUTPUT_DIR="$TMP_ROOT/all-reports"
        # shellcheck source=/dev/null
        . "$REPO_ROOT/scripts/security-scan.sh" all
        install_trivy() { :; }
        install_grype() { :; }
        install_syft() { :; }
        install_hadolint() { :; }
        check_dockle() { :; }
        generate_summary() { :; }
        scan_component() {
            local want
            for want in ${spec}; do
                if [ "${want%%:*}" = "$1" ]; then
                    return "${want##*:}"
                fi
            done
            return 0
        }
        main
    ) > "$out" 2>&1
}

rc=0
scan_all_with "" "$TMP_ROOT/all-clean.out" || rc=$?
if [ "$rc" -eq 0 ]; then
    ok "'all' with every component clean exits 0"
else
    bad "'all' with every component clean exited $rc; expected 0"
fi

rc=0
scan_all_with "backend:1 frontend:1" "$TMP_ROOT/all-findings.out" || rc=$?
if [ "$rc" -eq 1 ]; then
    ok "'all' with findings in two components exits 1, not 2 (no summing)"
else
    bad "'all' with findings in two components exited $rc; expected 1"
fi

rc=0
scan_all_with "docs:2" "$TMP_ROOT/all-cannot.out" || rc=$?
if [ "$rc" -eq 2 ]; then
    ok "'all' with one unscannable component exits 2 (could not scan)"
else
    bad "'all' with one unscannable component exited $rc; expected 2"
fi

if grep -qF "All security scans passed!" "$TMP_ROOT/all-cannot.out"; then
    bad "'all' claimed the scans passed while one component was never scanned"
else
    ok "'all' does not claim the scans passed when a component was never scanned"
fi

echo ""
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
