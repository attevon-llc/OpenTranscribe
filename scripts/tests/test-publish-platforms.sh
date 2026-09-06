#!/bin/bash
# Regression test for issue #680 — the published arm64 backend manifest was not
# equivalent to amd64 (765 MB vs 4,454 MB dependency layer, 8.4x smaller) and nothing
# in the release pipeline would have caught it: 80-publish.sh's old check only asked
# "does an arm64 manifest EXIST", never "is it the size a real CUDA build should be".
#
# Offline, fixture-driven, fast — modelled on scripts/tests/test-scan-not-a-pass.sh.
# No Docker, no network, no images. Uses REAL measured sizes from issue #680 (backend
# v0.4.1) and the #667 plan pass (frontend) as fixtures — see
# scripts/tests/fixtures/manifest-fixtures.py.
#
# Every assertion below is a MUST-FIRE or MUST-STAY-CLEAN case: each is run against a
# deliberately-broken state FIRST (the documented "red"), confirmed to fail for the
# stated reason, and only then re-run against the real/fixed state to confirm green.
# That is what "observe the red" means in this repo's testing convention (root
# CLAUDE.md) and it is not optional ceremony here — sections 5, 6 and 9 exist because a
# prior version of similar code looked reasonable and was wrong, and sections 12-19
# exist because the FIRST version of this gate would have failed every real publish
# (it fed a manifest INDEX, which carries no layers, to the size comparison).
#
# Deliberately no assertion count in this header: run the file, it prints one. The
# count here said "11" for three sections' worth of additions after it stopped being
# true, which is exactly how a measurement transcribed into prose rots.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FIXTURES="$SCRIPT_DIR/fixtures/manifest-fixtures.py"
CHECKER="$REPO_ROOT/scripts/lib/manifest_platform_check.py"

# Every repo file this suite makes an assertion ABOUT. This is a declared contract, not a
# comment: backend/tests/unit/test_precommit_hook_file_scope.py reads this array and fails if
# the `publish-platforms-not-a-pass` hook's `files:` pattern in .pre-commit-config.yaml does
# not select every entry — AND if this array omits a path the script actually reads.
#
# Why it exists: the hook's `files:` pattern went stale. The suite grew sections asserting on
# 90-promote.sh, 95-finish.sh, published-repos.sh, Dockerfile.blackwell, setup-opentranscribe.sh
# and asr/factory.py while the pattern still listed only the original six paths, so a commit
# touching ONLY 95-finish.sh — the file whose hardcoded repo list let a release publish :latest
# with no lite image on Docker Hub — would not have run this suite at all. A guard that does
# not select the file it guards is the same failure mode as a detector that matches nothing.
SUBJECT_FILES=(
    setup-opentranscribe.sh
    backend/Dockerfile.prod
    backend/Dockerfile.lite
    backend/Dockerfile.blackwell
    backend/app/services/asr/factory.py
    scripts/docker-build-push.sh
    scripts/security-scan.sh
    scripts/lib/manifest_platform_check.py
    scripts/release/80-publish.sh
    scripts/release/85-smoke.sh
    scripts/release/90-promote.sh
    scripts/release/95-finish.sh
    scripts/release/published-repos.sh
    scripts/tests/test-publish-platforms.sh
    scripts/tests/fixtures/manifest-fixtures.py
)
# Consumed by the unit test above; referenced here so shellcheck sees a use.
[ "${OT_PRINT_SUBJECT_FILES:-}" = "1" ] && { printf '%s\n' "${SUBJECT_FILES[@]}"; exit 0; }

pass=0
fail=0
ok() { echo "  ok   - $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL - $1"; fail=$((fail + 1)); }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

fixture() {
    python3 "$FIXTURES" "$1"
}

echo "== 1. list-platforms declares all legs with capability =="
plat_out="$(cd "$REPO_ROOT" && ./scripts/docker-build-push.sh list-platforms)"
# RED: prove this fails when a component is missing capability/platforms by
# checking a component list-platforms is NOT supposed to have any entry for.
if [ "$(echo "$plat_out" | grep -c "^nonexistent-component")" -gt 0 ]; then
    bad "sanity: list-platforms should not know a made-up component (this would be the red state if it did)"
else
    ok "RED confirmed: list-platforms has no entry for a nonexistent component"
fi
# GREEN: every declared component has a non-empty capability field; backend/lite/
# frontend/docs/blackwell must all be present.
missing_cap=0
for comp in backend lite frontend docs blackwell; do
    line="$(echo "$plat_out" | awk -F'\t' -v c="$comp" '$1==c')"
    if [ -z "$line" ]; then
        bad "list-platforms is missing component '$comp'"
        missing_cap=1
        continue
    fi
    cap="$(echo "$line" | cut -f2)"
    [ -n "$cap" ] || { bad "'$comp' has no capability"; missing_cap=1; }
done
[ "$missing_cap" -eq 0 ] && ok "list-platforms declares all 5 components with a non-empty capability"

echo "== 2. 80-publish.sh has no literal 'for arch in amd64 arm64' and invokes list-platforms =="
if grep -qE 'for[[:space:]]+arch[[:space:]]+in[[:space:]]+amd64[[:space:]]+arm64' "$REPO_ROOT/scripts/release/80-publish.sh"; then
    bad "80-publish.sh still contains the literal hardcoded arch loop"
else
    ok "80-publish.sh contains no literal 'for arch in amd64 arm64'"
fi
if grep -q 'list-platforms' "$REPO_ROOT/scripts/release/80-publish.sh"; then
    ok "80-publish.sh invokes list-platforms"
else
    bad "80-publish.sh never calls docker-build-push.sh list-platforms"
fi

echo "== 3. ratio check REJECTS the real v0.4.1 backend pair at bound 2.00 =="
fixture backend-v041-amd64 > "$TMP_ROOT/be-amd64.json"
fixture backend-v041-arm64 > "$TMP_ROOT/be-arm64.json"
rc=0
out="$(python3 "$CHECKER" check-ratio "$TMP_ROOT/be-amd64.json" "$TMP_ROOT/be-arm64.json" 2.00)" || rc=$?
if [ "$rc" -eq 0 ]; then
    bad "ratio check ACCEPTED the real v0.4.1 backend pair (should reject at 5.82 > 2.00): $out"
else
    ok "ratio check rejects the real v0.4.1 backend pair: $out"
fi
# RED confirmation: at an absurdly loose bound it must accept the SAME data —
# proves the rejection above is really about the bound, not a broken checker.
rc=0
out="$(python3 "$CHECKER" check-ratio "$TMP_ROOT/be-amd64.json" "$TMP_ROOT/be-arm64.json" 10.0)" || rc=$?
if [ "$rc" -eq 0 ]; then
    ok "RED confirmed: the same pair passes at a loose bound (10.0) — rejection at 2.00 is the bound, not a broken checker: $out"
else
    bad "the same pair should pass at bound 10.0 but did not: $out"
fi

echo "== 4. ratio check ACCEPTS the real frontend pair (12 layers each) at bound 1.25 =="
fixture frontend-amd64 > "$TMP_ROOT/fe-amd64.json"
fixture frontend-arm64 > "$TMP_ROOT/fe-arm64.json"
rc=0
out="$(python3 "$CHECKER" check-ratio "$TMP_ROOT/fe-amd64.json" "$TMP_ROOT/fe-arm64.json" 1.25)" || rc=$?
if [ "$rc" -eq 0 ]; then
    ok "ratio check accepts the real frontend pair: $out"
else
    bad "ratio check rejected the real frontend pair (should pass at 1.25): $out"
fi
# RED confirmation: an artificially tight bound must reject the SAME good pair.
rc=0
out="$(python3 "$CHECKER" check-ratio "$TMP_ROOT/fe-amd64.json" "$TMP_ROOT/fe-arm64.json" 1.001)" || rc=$?
if [ "$rc" -ne 0 ]; then
    ok "RED confirmed: the same good pair fails an artificially tight bound (1.001): $out"
else
    bad "the same pair should fail at bound 1.001 but passed: $out"
fi

echo "== 5. index set-equality rejects an EXTRA platform =="
fixture lite-index-extra-riscv64 > "$TMP_ROOT/idx-extra.json"
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/idx-extra.json" "linux/amd64,linux/arm64")" || rc=$?
if [ "$rc" -eq 0 ]; then
    bad "index check accepted an index with an undeclared extra platform (riscv64): $out"
else
    ok "index check rejects an extra undeclared platform: $out"
fi
# RED-confirmation-in-reverse: the CORRECT index (no extra platform) must pass the
# same declared set, proving the rejection above is about the extra platform.
fixture lite-index-correct > "$TMP_ROOT/idx-correct.json"
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/idx-correct.json" "linux/amd64,linux/arm64")" || rc=$?
[ "$rc" -eq 0 ] && ok "RED confirmed: the correct index (no extra) passes the same check: $out" \
    || bad "the correct index should pass but did not: $out"

echo "== 6. index set-equality rejects a MISSING platform =="
fixture lite-index-missing-arm64 > "$TMP_ROOT/idx-missing.json"
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/idx-missing.json" "linux/amd64,linux/arm64")" || rc=$?
if [ "$rc" -eq 0 ]; then
    bad "index check accepted an index missing arm64 — this is issue #680 itself: $out"
else
    ok "index check rejects a missing declared platform: $out"
fi

echo "== 7. a leg tag containing 2 platforms is rejected =="
fixture backend-leg-two-platforms > "$TMP_ROOT/leg-two.json"
rc=0
out="$(python3 "$CHECKER" check-leg "$TMP_ROOT/leg-two.json" "linux/amd64")" || rc=$?
if [ "$rc" -eq 0 ]; then
    bad "leg check accepted a tag declaring 2 platforms: $out"
else
    ok "leg check rejects a tag declaring more than one platform: $out"
fi
# RED confirmation: a genuine single-platform leg matching the expectation passes.
fixture backend-leg-amd64-only > "$TMP_ROOT/leg-one.json"
rc=0
out="$(python3 "$CHECKER" check-leg "$TMP_ROOT/leg-one.json" "linux/amd64")" || rc=$?
[ "$rc" -eq 0 ] && ok "RED confirmed: a real single-platform leg passes the same check: $out" \
    || bad "a real single-platform leg should pass but did not: $out"

echo "== 8. cross-purpose comparison is never attempted =="
# There is no code path in 80-publish.sh that feeds a backend (full/CUDA) manifest
# and a lite (CPU) manifest — or any two different REPO_FOR_COMPONENT entries — into
# the same check-ratio call. Grep for the one place ratio checks are invoked and
# confirm every pairing is within a single component's own amd64/arm64 legs.
if grep -q 'check-ratio' "$REPO_ROOT/scripts/release/80-publish.sh"; then
    # Extract the two manifest arguments per check-ratio call and confirm they
    # share a component prefix (derived from the same $component loop variable).
    bad_pairs=0
    while IFS= read -r line; do
        # A cross-purpose call would reference two DIFFERENT $repo/$component
        # variables on one line; every real call in 80-publish.sh instead uses the
        # SAME $component-scoped leg variables for both arguments.
        if [ "$(echo "$line" | grep -cE '\$\{?REPO_BACKEND\}?.*\$\{?REPO_BACKEND_LITE\}?|\$\{?REPO_BACKEND_LITE\}?.*\$\{?REPO_BACKEND\}?')" -gt 0 ]; then
            bad_pairs=$((bad_pairs + 1))
        fi
    done < <(grep 'check-ratio' "$REPO_ROOT/scripts/release/80-publish.sh")
    if [ "$bad_pairs" -eq 0 ]; then
        ok "no check-ratio call in 80-publish.sh mixes REPO_BACKEND and REPO_BACKEND_LITE"
    else
        bad "found $bad_pairs check-ratio call(s) that appear to mix backend and lite repos"
    fi
else
    bad "80-publish.sh never calls check-ratio at all — equivalence is not being checked"
fi

echo "== 9. every FROM ...@sha256: in Dockerfile.prod/Dockerfile.lite is multi-arch or per-TARGETARCH =="
check_dockerfile_arch_safety() {
    local dockerfile="$1"
    # A single bare `FROM x@sha256:... AS diar-native-bin` with no TARGETARCH
    # selection anywhere in the file is the RED state issue #680 found: BuildKit
    # accepts a --platform linux/arm64 build against a single-arch amd64 base with
    # only a warning, and ships an amd64 ELF under an arm64 tag.
    if grep -q 'ARG TARGETARCH' "$dockerfile" && grep -qE 'FROM .*-\$\{TARGETARCH\}' "$dockerfile"; then
        return 0
    fi
    return 1
}
if check_dockerfile_arch_safety "$REPO_ROOT/backend/Dockerfile.lite"; then
    ok "Dockerfile.lite selects diar-native-bin per TARGETARCH"
else
    bad "Dockerfile.lite has no per-TARGETARCH diar-native selection"
fi
if check_dockerfile_arch_safety "$REPO_ROOT/backend/Dockerfile.prod"; then
    ok "Dockerfile.prod selects diar-native-bin per TARGETARCH (arm64 leg intentionally unresolvable)"
else
    bad "Dockerfile.prod has no per-TARGETARCH diar-native selection"
fi
# RED: reverting to the pre-#680 single bare FROM (no ARG TARGETARCH, no per-arch
# stage) must fail the same check — proves the assertion actually distinguishes them.
cat > "$TMP_ROOT/Dockerfile.pre680" << 'EOF'
FROM davidamacey/diar-native@sha256:83a709be94d0ca06441fa10aea0680f53b03cc10eb3ce11c4eeb84478400567d AS diar-native-bin
FROM python:3.13-slim-trixie AS builder
EOF
if check_dockerfile_arch_safety "$TMP_ROOT/Dockerfile.pre680"; then
    bad "the pre-#680 single-arch-only Dockerfile shape incorrectly passed the safety check"
else
    ok "RED confirmed: the pre-#680 single bare FROM (no TARGETARCH) fails the same check"
fi

echo "== 10. platform-table keys == security-scan.sh list-components =="
scan_known="$(cd "$REPO_ROOT" && ./scripts/security-scan.sh list-components | sort | tr '\n' ' ')"
table_known="$(echo "$plat_out" | cut -f1 | sort | tr '\n' ' ')"
if [ "$scan_known" = "$table_known" ]; then
    ok "docker-build-push.sh's platform table keys match security-scan.sh's list-components: '${scan_known% }'"
else
    bad "key sets differ — scan: '$scan_known' vs table: '$table_known'"
fi
# RED: assert_platform_table_matches_scan_components must itself fail when the
# tables disagree. Exercise it directly by sourcing docker-build-push.sh with a
# deliberately mismatched COMPONENT_PLATFORMS.
red_out="$(
    cd "$REPO_ROOT" || exit 99
    # shellcheck source=/dev/null
    . ./scripts/docker-build-push.sh
    # Consumed by assert_platform_table_matches_scan_components, which reads this
    # global associative array by name after being sourced above — shellcheck
    # cannot see that cross-function use.
    # shellcheck disable=SC2034
    COMPONENT_PLATFORMS=([backend]="linux/amd64")  # drop lite/frontend/docs/blackwell
    assert_platform_table_matches_scan_components
)"
red_rc=$?
if [ "$red_rc" -ne 0 ]; then
    ok "RED confirmed: assert_platform_table_matches_scan_components fails on a deliberately mismatched table"
else
    bad "assert_platform_table_matches_scan_components should have failed on a mismatched table: $red_out"
fi

echo "== 11. fixture parser ignores architecture: \"unknown\" attestation entries =="
fixture lite-index-with-attestation > "$TMP_ROOT/idx-attest.json"
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/idx-attest.json" "linux/amd64,linux/arm64")" || rc=$?
if [ "$rc" -eq 0 ]; then
    ok "index check ignores an architecture:unknown attestation entry and still matches amd64+arm64: $out"
else
    bad "index check incorrectly counted an attestation entry as a real platform: $out"
fi
# RED: without the unknown-arch guard, an attestation-bearing index would report
# THREE platforms (amd64, arm64, unknown/unknown) and reject as "extra" — prove this
# by calling platform_set() directly and checking "unknown/unknown" is absent.
red_check="$(python3 -c "
import sys, json
sys.path.insert(0, '$REPO_ROOT/scripts/lib')
import manifest_platform_check as m
doc = json.load(open('$TMP_ROOT/idx-attest.json'))
platforms = m.platform_set(doc)
assert 'unknown/unknown' not in platforms, f'attestation entry leaked into platform_set: {platforms}'
print('ok')
")"
if [ "$red_check" = "ok" ]; then
    ok "RED confirmed: platform_set() does not leak the attestation entry as unknown/unknown"
else
    bad "platform_set() leaked the attestation entry: $red_check"
fi

echo "== 12. an INDEX fed to check-ratio is CANNOT-CHECK (3), never a ratio verdict =="
# This is the shape 80-publish.sh writes to disk: `imagetools inspect <tag> --raw`
# returns an index with NO `layers` and a per-entry `size` of the ~2.4 KB manifest
# blob. The first revision of the checker summed absent layers to 0 and reported
# "ratio=inf exceeds bound" — a verdict drawn from data it never had, which fails
# even when comparing a healthy index against ITSELF. Measured against the real
# published opentranscribe-backend:v0.4.1 index before this case existed.
fixture real-index-amd64-arm64 > "$TMP_ROOT/real-idx.json"
rc=0
out="$(python3 "$CHECKER" check-ratio "$TMP_ROOT/real-idx.json" "$TMP_ROOT/real-idx.json" 1.25)" || rc=$?
if [ "$rc" -eq 3 ]; then
    ok "check-ratio on an index reports CANNOT CHECK (exit 3): $out"
elif [ "$rc" -eq 0 ]; then
    bad "check-ratio ACCEPTED an index it cannot measure — a pass drawn from absent data: $out"
else
    bad "check-ratio on an index exited $rc (expected 3 = cannot check, not $rc = mismatch): $out"
fi
# RED confirmation: the SAME call on the per-platform manifests the index points at
# must produce a real verdict, proving exit 3 above is about the document shape.
fixture real-manifest-cpu-a > "$TMP_ROOT/real-cpu-a.json"
fixture real-manifest-cpu-b > "$TMP_ROOT/real-cpu-b.json"
rc=0
out="$(python3 "$CHECKER" check-ratio "$TMP_ROOT/real-cpu-a.json" "$TMP_ROOT/real-cpu-b.json" 1.25)" || rc=$?
[ "$rc" -eq 0 ] && ok "RED confirmed: the resolved per-platform manifests DO produce a verdict: $out" \
    || bad "the resolved per-platform manifest pair should pass at 1.25 but exited $rc: $out"
# ...and the real #680 pair, in the same real shape, is still rejected.
fixture real-manifest-amd64 > "$TMP_ROOT/real-be-amd64.json"
fixture real-manifest-arm64 > "$TMP_ROOT/real-be-arm64.json"
rc=0
out="$(python3 "$CHECKER" check-ratio "$TMP_ROOT/real-be-amd64.json" "$TMP_ROOT/real-be-arm64.json" 2.00)" || rc=$?
[ "$rc" -eq 1 ] && ok "the real #680 v0.4.1 pair is rejected as a MISMATCH (exit 1) in the real doc shape: $out" \
    || bad "expected exit 1 (mismatch) for the real #680 pair, got $rc: $out"

echo "== 13. a document that declares no platform is CANNOT-CHECK, not '0 platforms' =="
# A bare image manifest (what a builder that pushes no provenance leaves behind)
# carries its architecture in the config blob, which --raw does not fetch. Reporting
# "leg tag declares 0 platform(s)" for one would be a verdict from absent data.
rc=0
out="$(python3 "$CHECKER" check-leg "$TMP_ROOT/real-cpu-a.json" "linux/amd64")" || rc=$?
if [ "$rc" -eq 3 ]; then
    ok "check-leg on a platform-less image manifest reports CANNOT CHECK (exit 3): $out"
else
    bad "expected exit 3 (cannot check) for a platform-less manifest, got $rc: $out"
fi
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/real-cpu-a.json" "linux/amd64,linux/arm64")" || rc=$?
[ "$rc" -eq 3 ] && ok "check-index on a platform-less image manifest also reports CANNOT CHECK: $out" \
    || bad "expected exit 3 from check-index on a platform-less manifest, got $rc: $out"

echo "== 14. malformed / empty input is CANNOT-CHECK (3), not a mismatch (1) =="
printf 'not json at all' > "$TMP_ROOT/garbage.json"
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/garbage.json" "linux/amd64")" || rc=$?
[ "$rc" -eq 3 ] && ok "malformed JSON exits 3 (cannot check): $out" \
    || bad "malformed JSON should exit 3, got $rc: $out"
: > "$TMP_ROOT/empty.json"
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/empty.json" "linux/amd64")" || rc=$?
[ "$rc" -eq 3 ] && ok "an empty inspect result exits 3 (cannot check): $out" \
    || bad "an empty inspect result should exit 3, got $rc: $out"
rc=0
out="$(python3 "$CHECKER" check-index "$TMP_ROOT/does-not-exist.json" "linux/amd64")" || rc=$?
[ "$rc" -eq 3 ] && ok "a missing file exits 3 (cannot check): $out" \
    || bad "a missing file should exit 3, got $rc: $out"

echo "== 15. resolve-digest turns an index into the per-platform manifest digest =="
rc=0
dig="$(python3 "$CHECKER" resolve-digest "$TMP_ROOT/real-idx.json" "linux/arm64")" || rc=$?
expect_arm="sha256:$(python3 -c "print(('arm64'.encode().hex()*16)[:64])")"
if [ "$rc" -eq 0 ] && [ "$dig" = "$expect_arm" ]; then
    ok "resolve-digest returns the arm64 entry's digest: $dig"
else
    bad "resolve-digest arm64 gave rc=$rc digest='$dig', expected '$expect_arm'"
fi
# Never the attestation entry, and never a platform the index does not declare.
rc=0
out="$(python3 "$CHECKER" resolve-digest "$TMP_ROOT/real-idx.json" "unknown/unknown")" || rc=$?
[ "$rc" -eq 1 ] && ok "resolve-digest refuses the attestation entry (exit 1, mismatch): $out" \
    || bad "resolve-digest should not resolve unknown/unknown, got rc=$rc: $out"
rc=0
out="$(python3 "$CHECKER" resolve-digest "$TMP_ROOT/real-cpu-a.json" "linux/amd64")" || rc=$?
[ "$rc" -eq 3 ] && ok "resolve-digest on a non-index reports CANNOT CHECK (exit 3): $out" \
    || bad "resolve-digest on a non-index should exit 3, got $rc: $out"

echo "== 16. 80-publish.sh resolves per-platform manifests before comparing sizes =="
# Structural, because the functional half needs a registry: the ratio check must be
# fed repo@<digest> documents, so `resolve-digest` has to appear on the same path as
# `check-ratio`. Without it the stage inspects the index and compares nothing.
if grep -q 'resolve-digest' "$REPO_ROOT/scripts/release/80-publish.sh"; then
    ok "80-publish.sh resolves per-platform digests before check-ratio"
else
    bad "80-publish.sh calls check-ratio without ever resolving a per-platform digest — it is comparing indexes"
fi
# The ratio check must also cover frontend/docs, whose 1.25 bound releasing.md
# documents; gating it on leg tags alone silently exempted them.
if grep -qE 'leg_files\[@\]\}? -eq 2' "$REPO_ROOT/scripts/release/80-publish.sh"; then
    bad "the equivalence check is still gated on leg-tag count, so frontend/docs are exempt from it"
else
    ok "the equivalence check is not gated on capability leg tags (frontend/docs are covered)"
fi

echo "== 17. 85-smoke's capability probe cannot PASS on a run that never happened =="
# The lite assertion is "torch.version.cuda is EMPTY". An image that cannot execute at
# all — wrong-arch ELF, no python3, failed pull — also yields an empty string, so a
# probe that discards docker's exit status turns "could not check" into "CPU-only
# confirmed". That is the #680 failure mode wearing a green tick, in the one stage
# whose whole job is to catch it.
SMOKE="$REPO_ROOT/scripts/release/85-smoke.sh"
if grep -q 'run_rc' "$SMOKE" && grep -q 'could NOT run the capability probe' "$SMOKE"; then
    ok "85-smoke captures the capability probe's exit status and fails on a bad run"
else
    bad "85-smoke's capability probe does not check docker's exit status — an unrunnable image passes the lite assertion"
fi
# The pre-fix probe was `python3 -c 'import torch; ...' 2>/dev/null | tr -d '\r'` on one
# line — stderr AND (through the pipe) the exit status both discarded. Assert that exact
# shape is gone; this fires on the committed pre-fix file, which is what makes it a test
# rather than a restatement of the line above.
if grep -q "torch.version.cuda.*2>/dev/null" "$SMOKE"; then
    bad "85-smoke still reads torch.version.cuda from a run whose stderr and exit status are discarded"
else
    ok "the exit-status-discarding probe form is gone from 85-smoke"
fi

echo "== 18. check (a) can still VERIFY a leg that declares no platform in its manifest =="
# A leg tag pushed without provenance attestations resolves to a BARE IMAGE MANIFEST,
# which declares no platform at all. check-leg can only answer CANNOT CHECK for that
# shape (assertion 13) — so without a fallback, check (a) would be permanently
# inconclusive whenever attestations are off, and "a check that can never pass" is not
# a gate. The fallback reads the CONFIG BLOB. Verified against the real published
# davidamacey/diar-native:0.3.1-cpu: its --raw declares no platform, while
# `--format '{{json .Image}}'` reports os=linux architecture=amd64.
fixture image-config-amd64 > "$TMP_ROOT/cfg-amd64.json"
rc=0
out="$(python3 "$CHECKER" check-image-config "$TMP_ROOT/cfg-amd64.json" "linux/amd64")" || rc=$?
if [ "$rc" -eq 0 ]; then
    ok "a config blob VERIFIES the declared platform (exit 0): $out"
else
    bad "check-image-config could not verify a matching config blob (rc=$rc): $out"
fi
# RED 1: the same code path must REJECT a config blob for the wrong architecture —
# otherwise the fallback would be a rubber stamp that turns every leg green.
rc=0
out="$(python3 "$CHECKER" check-image-config "$TMP_ROOT/cfg-amd64.json" "linux/arm64")" || rc=$?
if [ "$rc" -eq 1 ]; then
    ok "RED confirmed: an amd64 config blob is REJECTED (exit 1) when arm64 was declared: $out"
else
    bad "an amd64 config blob checked against linux/arm64 should exit 1, got rc=$rc: $out"
fi
# RED 2: a config blob carrying no platform fields must be CANNOT CHECK (3), not a pass.
fixture image-config-empty > "$TMP_ROOT/cfg-empty.json"
rc=0
out="$(python3 "$CHECKER" check-image-config "$TMP_ROOT/cfg-empty.json" "linux/amd64")" || rc=$?
if [ "$rc" -eq 3 ]; then
    ok "RED confirmed: a platform-less config blob is CANNOT CHECK (exit 3), not a pass: $out"
else
    bad "a platform-less config blob should exit 3 (cannot check), got rc=$rc: $out"
fi

echo "== 19. 80-publish.sh actually WIRES that fallback into check (a) =="
# Static: the fallback must be reachable from the leg loop, keyed on the CANNOT-CHECK
# exit specifically. Gating it on any non-zero rc would swallow a genuine mismatch
# (exit 1) and re-check it via a different route, turning a real #680-class finding
# into a pass — so assert on the exact `-eq 3` guard, not merely on the subcommand.
if grep -q 'check-image-config' "$REPO_ROOT/scripts/release/80-publish.sh"; then
    ok "80-publish.sh calls check-image-config"
else
    bad "80-publish.sh never calls check-image-config — check (a) stays inconclusive without attestations"
fi
if grep -qE 'leg_rc -eq 3' "$REPO_ROOT/scripts/release/80-publish.sh"; then
    ok "the fallback is gated on the CANNOT-CHECK exit (3) specifically, not on any failure"
else
    bad "the config-blob fallback is not gated on exit 3 — a real mismatch (exit 1) could be re-checked into a pass"
fi

echo "== 20. every Dockerfile handed the identity build args DECLARES all three (#667) =="
# build_backend_identity_args() passes --build-arg APP_VERSION / GIT_SHA / BUILD_TIME to
# every capability-bearing backend component. A Dockerfile that does not declare an ARG
# still BUILDS — buildx only warns — so the failure is silent and shows up as a running
# container reporting GIT_SHA=unknown from GET /version. Dockerfile.lite and
# Dockerfile.blackwell each declared APP_VERSION alone until #667.
#
# Derived from the script, not transcribed: the arg names come out of the real function
# body, so adding a fourth identity arg without declaring it anywhere fails here.
identity_args="$(grep -A6 'build_backend_identity_args()' "$REPO_ROOT/scripts/docker-build-push.sh" \
                 | grep -oE '"--build-arg" "[A-Z_]+=' | grep -oE '[A-Z_]+' | grep -v BUILD_ARG | sort -u)"
if [ -z "$identity_args" ]; then
    bad "could not extract the identity build args from docker-build-push.sh (cannot check)"
else
    ok "extracted identity build args from the real function: $(echo "$identity_args" | tr '\n' ' ')"
    for df in Dockerfile.prod Dockerfile.lite Dockerfile.blackwell; do
        undeclared=""
        for a in $identity_args; do
            grep -qE "^ARG[[:space:]]+${a}(=|$)" "$REPO_ROOT/backend/$df" || undeclared="$undeclared $a"
        done
        if [ -n "$undeclared" ]; then
            bad "backend/$df is passed but does not declare:$undeclared (it will silently bake 'unknown')"
        else
            ok "backend/$df declares every identity build arg it is passed"
        fi
    done
    # MUST-FIRE control: the detector must actually notice an undeclared arg. A synthetic
    # Dockerfile declaring only APP_VERSION is the exact pre-#667 state of Dockerfile.lite.
    printf 'FROM scratch\nARG APP_VERSION=unknown\n' > "$TMP_ROOT/Dockerfile.redcheck"
    red_undeclared=""
    for a in $identity_args; do
        grep -qE "^ARG[[:space:]]+${a}(=|$)" "$TMP_ROOT/Dockerfile.redcheck" || red_undeclared="$red_undeclared $a"
    done
    if [ -n "$red_undeclared" ]; then
        ok "RED confirmed: the check fires on a Dockerfile declaring only APP_VERSION (missing:$red_undeclared)"
    else
        bad "detector matched nothing against a deliberately-incomplete Dockerfile — it cannot fail"
    fi
fi

echo "== 21. the identity ARGs sit AFTER the expensive layers, not at the top of the stage =="
# BUILD_TIME changes on every single build and an ENV layer invalidates the remainder of
# its stage, so declaring these above `COPY --from=builder`/`COPY . .` re-runs apt, the
# useradd and a ~GB dependency copy on every build. Dockerfile.prod documents this; the
# fix for #667 must not reintroduce it in the other two. Assert on ORDER, not presence.
for df in Dockerfile.prod Dockerfile.lite Dockerfile.blackwell; do
    bt_line="$(grep -nE '^ARG[[:space:]]+BUILD_TIME' "$REPO_ROOT/backend/$df" | head -1 | cut -d: -f1)"
    copy_line="$(grep -nE '^COPY (--chown=[^ ]+ )?\. \.' "$REPO_ROOT/backend/$df" | head -1 | cut -d: -f1)"
    if [ -z "$bt_line" ] || [ -z "$copy_line" ]; then
        bad "backend/$df: could not locate both the BUILD_TIME ARG and the app COPY (cannot check ordering)"
    elif [ "$bt_line" -gt "$copy_line" ]; then
        ok "backend/$df declares BUILD_TIME (line $bt_line) after the app COPY (line $copy_line)"
    else
        bad "backend/$df declares BUILD_TIME at line $bt_line, ABOVE the app COPY at $copy_line — every build busts the cache below it"
    fi
done

echo "== 22. BUILD_MODE=local can target a NON-HOST arch, so arm64 is verifiable pre-publish =="
# Before #667, build_platforms() ignored PLATFORMS entirely in local mode and always
# returned the host arch. Consequence: the ONLY way to obtain an arm64 leg was
# `buildx --push`, i.e. the first inspection of an arm64 artifact happened after it was
# already on Docker Hub under a moving tag — which is how #680's broken arm64 backend
# shipped. Source the real script (it guards main() behind BASH_SOURCE) and call the real
# function; no Docker needed, because the override path never shells out.
# NOTE: the env vars must be EXPORTED, not prefixed onto `source`. A prefix assignment is
# scoped to that one command, so `BUILD_MODE=local source ...` leaves BUILD_MODE unset by
# the time build_platforms runs — which under the script's `set -u` aborts with "unbound
# variable" rather than returning a wrong answer. (Observed while writing this test.)
platfn="$(
    cd "$REPO_ROOT" || exit 1
    export BUILD_MODE=local PLATFORMS=linux/arm64
    # shellcheck disable=SC1091  # path is computed; this sources the real script under test
    source ./scripts/docker-build-push.sh >/dev/null 2>&1
    build_platforms lite
)"
if [ "$platfn" = "linux/arm64" ]; then
    ok "local mode honours PLATFORMS=linux/arm64 (got '$platfn'), so the arm64 leg is buildable without pushing"
else
    bad "local mode returned '$platfn' for PLATFORMS=linux/arm64 — the non-host leg is unreachable without --push"
fi
# MUST-STAY-CLEAN: with PLATFORMS unset, local mode must still follow the daemon, or
# pointing DOCKER_CONTEXT at a native arm64 builder would stop working.
if [ "$(grep -A12 '^build_platforms()' "$REPO_ROOT/scripts/docker-build-push.sh" \
     | grep -c "docker version --format")" -gt 0 ]; then
    ok "with PLATFORMS unset, local mode still derives the arch from the daemon it is pointed at"
else
    bad "local mode no longer falls back to the daemon's arch — DOCKER_CONTEXT=remote-arm64 would break"
fi

echo "== 23. BUILD_MODE=local + multi-platform PLATFORMS is refused up front, not deep in buildx =="
# --load genuinely cannot export a multi-arch manifest. Failing at the precondition costs
# nothing; failing inside buildx costs both builds first. Exit 2 = misuse, per the release
# pipeline's stable exit codes.
out="$(cd "$REPO_ROOT" && BUILD_MODE=local PLATFORMS=linux/amd64,linux/arm64 \
        ./scripts/docker-build-push.sh lite 2>&1)"
rc=$?
if [ "$rc" -eq 2 ]; then
    ok "multi-platform local build is refused with exit 2 (misuse)"
else
    bad "multi-platform local build exited $rc, expected 2 — it would fail inside buildx after building"
fi
if [ "$(echo "$out" | grep -c "cannot export a multi-arch manifest")" -gt 0 ]; then
    ok "the refusal names the actual reason"
else
    bad "the refusal does not explain why (operator has to read the source)"
fi
# MUST-STAY-CLEAN: a SINGLE platform must NOT be refused — that is the whole point of 22.
(cd "$REPO_ROOT" && BUILD_MODE=local PLATFORMS=linux/arm64 DRY_RUN=true \
    ./scripts/docker-build-push.sh list-platforms >/dev/null 2>&1)
if [ $? -ne 2 ]; then
    ok "a single-platform local build is NOT refused"
else
    bad "the guard also rejects a single platform — it would block the pre-publish arm64 build it exists to enable"
fi

echo "== 24. promote and finish DERIVE their repo list and both cover lite (#667) =="
# 90-promote.sh listed `backend backend-lite frontend docs`; 95-finish.sh listed
# `backend frontend`. The two disagreed, and finish — the stage that publishes the
# GitHub Release as --latest — never asked whether the lite image existed at all. On
# arm64 that is a total install failure, since opentranscribe.sh defaults arm64 hosts
# to DEPLOYMENT_MODE=lite and the lite image is the only backend they can pull.
for stage in 90-promote.sh 95-finish.sh; do
    # Strip comments before grepping: both files QUOTE the old hardcoded loops in the
    # comment explaining why they are gone, and matching those would make the check
    # permanently red for documenting itself.
    if [ "$(sed 's/#.*//' "$REPO_ROOT/scripts/release/$stage" | grep -cE 'for repo in [a-z]')" -gt 0 ]; then
        bad "$stage still hardcodes its repo list — it will drift from the component table again"
    else
        ok "$stage has no hardcoded 'for repo in ...' list in executable code"
    fi
    if grep -q 'published-repos.sh' "$REPO_ROOT/scripts/release/$stage"; then
        ok "$stage derives its repo list from published-repos.sh"
    else
        bad "$stage does not source published-repos.sh — its list is not derived"
    fi
done
# The derivation must actually yield lite, and must NOT yield blackwell.
derived="$(cd "$REPO_ROOT" && source ./scripts/release/published-repos.sh && release_published_repos)"
if [ "$(echo "$derived" | grep -cE '^lite	.*opentranscribe-backend-lite$')" -gt 0 ]; then
    ok "the derived list includes lite -> opentranscribe-backend-lite"
else
    bad "the derived list does not include the lite repo — finish would pass with no lite image published"
fi
if [ "$(echo "$derived" | grep -c '^blackwell	')" -gt 0 ]; then
    bad "the derived list includes blackwell, which publishes no :vX.Y.Z tag — every release would fail"
else
    ok "blackwell is excluded (it publishes a :blackwell tag, not a versioned one)"
fi
# MUST-FIRE: an empty derivation is CANNOT-CHECK (exit 3), never a silent zero-iteration pass.
(
    cd "$TMP_ROOT" || exit 9
    mkdir -p scripts/release scripts
    cp "$REPO_ROOT/scripts/release/published-repos.sh" scripts/release/
    printf '#!/bin/bash\nexit 0\n' > scripts/security-scan.sh   # emits nothing
    chmod +x scripts/security-scan.sh
    # EXPORTED, not prefixed — a prefix assignment on `source` does not survive into the
    # later function call, so the function would resolve the REAL security-scan.sh and
    # this must-fire case would silently pass for the wrong reason.
    export REPO_ROOT="$TMP_ROOT"
    # shellcheck disable=SC1091  # fixture copy of the file under test
    source scripts/release/published-repos.sh
    release_published_repos_or_die >/dev/null 2>&1
)
if [ $? -eq 3 ]; then
    ok "RED confirmed: an empty component list exits 3 (could-not-check), not 0 (nothing to check)"
else
    bad "an empty component list did not exit 3 — a broken security-scan.sh would green-light an unverified release"
fi

# ...and the CALL SITE must propagate that, which is a separate fact.
#
# The first version of these stages consumed the helper as `done < <(release_published_
# repos_or_die)`. Process substitution runs in a subshell, so `exit 3` ended only that
# subshell: the loop read zero lines, the accumulator stayed empty, and the stage PASSED —
# the exact silent-zero-iteration bug the helper exists to prevent, reintroduced at its own
# call site, with the helper's own unit case still green. Assert on the shape.
for stage in 90-promote.sh 95-finish.sh; do
    # Comments stripped: 95-finish.sh quotes the broken `done < <(...)` form in the note
    # explaining why it is not used, and matching that would make this permanently red.
    if [ "$(sed 's/#.*//' "$REPO_ROOT/scripts/release/$stage" \
         | grep -cE 'done[[:space:]]*<[[:space:]]*<\([[:space:]]*release_published_repos')" -gt 0 ]; then
        bad "$stage pipes the helper in via process substitution — its exit 3 cannot reach the stage, so an underivable list PASSES"
    else
        ok "$stage does not consume the helper through a subshell"
    fi
    # The rc must REACH an exit. Two shapes are legitimate and the check accepts both,
    # because pinning the literal `|| exit $?` would have failed a change that is strictly
    # better: both stages now capture the rc so they can `record` the criterion that says
    # WHY the list could not be derived, and then exit that same rc.
    #
    #   repos_tsv="$(release_published_repos_or_die)" || exit $?
    #   repos_tsv="$(release_published_repos_or_die)" || list_rc=$?   ... exit "$list_rc"
    #
    # What is NOT accepted is a bare capture with no `||` at all — that is the regression
    # this case exists for, and the RED control below proves the check still sees it.
    stage_code="$(sed 's/#.*//' "$REPO_ROOT/scripts/release/$stage")"
    propagates=no
    # `grep -c ... -gt 0`, never `| grep -qE ...` — the rule test-scan-not-a-pass.sh states
    # and this file is named as the sibling of. Under this script's `set -o pipefail`, grep -q
    # exits on its first match, the producer dies with SIGPIPE, and the `if` sees FAILURE for
    # a pattern that MATCHED. Here that inverts "the stage propagates its could-not-derive
    # exit" into "it does not" for the first shape and then falls through to the second,
    # i.e. a silently size-dependent check of a release gate.
    if [ "$(printf '%s\n' "$stage_code" \
         | grep -cE 'repos_tsv="\$\(release_published_repos_or_die\)"[[:space:]]*\|\|[[:space:]]*exit')" -gt 0 ]; then
        propagates=direct
    else
        rc_var="$(printf '%s\n' "$stage_code" \
            | sed -nE 's/.*repos_tsv="\$\(release_published_repos_or_die\)"[[:space:]]*\|\|[[:space:]]*([A-Za-z_][A-Za-z_0-9]*)=\$\?.*/\1/p' \
            | head -1)"
        if [ -n "$rc_var" ] && [ "$(printf '%s\n' "$stage_code" \
             | grep -cE "(exit|fail_out)[[:space:]]+\"?\\\$(\{)?${rc_var}(\})?\"?")" -gt 0 ]; then
            propagates="via \$${rc_var}"
        fi
    fi
    if [ "$propagates" != no ]; then
        ok "$stage captures the list and propagates the could-not-derive exit ($propagates)"
    else
        bad "$stage does not propagate the helper's exit status — an underivable list would not fail the stage"
    fi
done

# MUST-FIRE control for the check above. Without it, a mistake in that multi-branch regex
# reports "propagates" for every shape, including the bare capture that started this — and a
# check that cannot fail is worse here than no check, because the thing it guards (a release
# proceeding over an underivable image list) is silent by construction.
control_bad='repos_tsv="$(release_published_repos_or_die)"
while IFS= read -r line; do :; done <<< "$repos_tsv"'
control_ok_direct='repos_tsv="$(release_published_repos_or_die)" || exit $?'
control_ok_var='repos_tsv="$(release_published_repos_or_die)" || list_rc=$?
exit "$list_rc"'
check_propagation() {   # echoes yes/no for a code string
    # Must stay byte-for-byte the same shape as the loop above, `grep -c` included: this is
    # the control that proves that check can fail, so a divergence here means the control is
    # exercising a different predicate than the thing it controls.
    local code="$1" rc_var
    if [ "$(printf '%s\n' "$code" \
         | grep -cE 'repos_tsv="\$\(release_published_repos_or_die\)"[[:space:]]*\|\|[[:space:]]*exit')" -gt 0 ]; then
        echo yes; return
    fi
    rc_var="$(printf '%s\n' "$code" \
        | sed -nE 's/.*repos_tsv="\$\(release_published_repos_or_die\)"[[:space:]]*\|\|[[:space:]]*([A-Za-z_][A-Za-z_0-9]*)=\$\?.*/\1/p' \
        | head -1)"
    if [ -n "$rc_var" ] && [ "$(printf '%s\n' "$code" \
         | grep -cE "(exit|fail_out)[[:space:]]+\"?\\\$(\{)?${rc_var}(\})?\"?")" -gt 0 ]; then
        echo yes; return
    fi
    echo no
}
if [ "$(check_propagation "$control_bad")" = no ]; then
    ok "RED confirmed: a bare capture with no || is reported as NOT propagating"
else
    bad "the propagation check passes a bare capture — it would not catch the original bug"
fi
if [ "$(check_propagation "$control_ok_direct")" = yes ] && [ "$(check_propagation "$control_ok_var")" = yes ]; then
    ok "both legitimate propagation shapes are accepted"
else
    bad "the propagation check rejects a shape that does propagate the exit status"
fi

echo "== 25. the installer can select the lite deployment, and rejects what it cannot do (#667) =="
INSTALLER="$REPO_ROOT/setup-opentranscribe.sh"
if grep -qE '^[[:space:]]+--lite\)' "$INSTALLER"; then
    ok "setup-opentranscribe.sh has a --lite arm"
else
    bad "setup-opentranscribe.sh has no --lite arm — DEPLOYMENT_MODE=lite is unreachable from a curl install"
fi
if grep -q '_upsert_env "DEPLOYMENT_MODE" "lite"' "$INSTALLER"; then
    ok "--lite PERSISTS DEPLOYMENT_MODE=lite into .env (not just this run)"
else
    bad "--lite does not persist DEPLOYMENT_MODE — opentranscribe.sh would select the full overlay on the next start"
fi
# The selector string must be the exact one the ASR guard compares against, or the stack
# runs the lite image while the code believes a local model is available.
guard_str="$(grep -oE 'DEPLOYMENT_MODE\.lower\(\) == "[a-z]+"' \
             "$REPO_ROOT/backend/app/services/asr/factory.py" | grep -oE '"[a-z]+"' | tr -d '"')"
if [ "$guard_str" = "lite" ]; then
    ok "asr/factory.py's guard compares against the same literal 'lite' the installer writes"
else
    bad "asr/factory.py gates on '$guard_str' but the installer writes 'lite' — overlay and guard have diverged"
fi
# Unknown arguments must be FATAL. Warn-and-continue is what silently performed a FULL
# install for anyone who passed --lite before the flag existed.
"$INSTALLER" --definitely-not-a-real-flag >/dev/null 2>&1
if [ $? -eq 2 ]; then
    ok "an unknown installer argument exits 2 instead of warning and installing something else"
else
    bad "an unknown installer argument is still non-fatal — a user gets a shape they did not ask for"
fi
# MUST-STAY-CLEAN: --help must still work and must advertise --lite.
help_out="$("$INSTALLER" --help 2>&1)"
if [ "$(echo "$help_out" | grep -c -- '--lite')" -gt 0 ]; then
    ok "--help advertises --lite"
else
    bad "--lite is undocumented in --help, so nobody will find it"
fi

echo ""
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
