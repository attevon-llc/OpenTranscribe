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
if echo "$plat_out" | grep -q "^nonexistent-component"; then
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
        if echo "$line" | grep -qE '\$\{?REPO_BACKEND\}?.*\$\{?REPO_BACKEND_LITE\}?|\$\{?REPO_BACKEND_LITE\}?.*\$\{?REPO_BACKEND\}?'; then
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

echo ""
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
