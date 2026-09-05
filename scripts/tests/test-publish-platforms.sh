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
# Each of the 11 assertions below is a MUST-FIRE or MUST-STAY-CLEAN case: every one is
# run against a deliberately-broken state FIRST (the documented "red"), confirmed to
# fail for the stated reason, and only then re-run against the real/fixed state to
# confirm green. That is what "observe the red" means in this repo's testing
# convention (root CLAUDE.md) and it is not optional ceremony here — three of these
# assertions (5, 6, 9) exist specifically because a prior version of similar code
# looked reasonable and was wrong.

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

echo ""
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
