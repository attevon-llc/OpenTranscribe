#!/bin/bash
# Preflight — every check that can run before anything is built.
#
# Exists so a release fails in seconds rather than 45 minutes into a 13.8 GB
# build. Each check here has, at some point, been the thing that went wrong:
# a stale remote-builder IP, a dirty worktree, a missing HUGGINGFACE_TOKEN, a
# scanner that was never installed.
#
# Read-only. Starts nothing, builds nothing, writes nothing outside .release/.
#
# Exit: 0 pass · 1 a blocking check failed · 2 this script and
#       release-criteria.yaml disagree about which criteria exist · 3
#       precondition unmet

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

PASS=0; FAIL=0; WARN=0
RESULTS=()

# ── Criteria ───────────────────────────────────────────────────────────────
# Severity is NOT decided here. This script decides whether each check PASSED;
# release-criteria.yaml decides whether a failure stops the release. They used
# to both carry an opinion and disagree (`remote-builder` was `blocking` in the
# YAML and `warn` here) with nothing to notice, because nothing read the YAML.
STAGE_ID=preflight
CRITERIA_YAML=scripts/release/release-criteria.yaml
# The repo venv has PyYAML; bare python3 usually does too. Either is fine, and
# an unreadable criteria file is a precondition failure, never a silent pass.
CRITERIA_PY=python3
[[ -x backend/venv/bin/python ]] && CRITERIA_PY=backend/venv/bin/python

declare -A SEVERITY=()
declare -A RECORDED=()

while IFS='|' read -r _id _sev; do
    [[ -n "$_id" ]] && SEVERITY["$_id"]="$_sev"
done < <("$CRITERIA_PY" - "$CRITERIA_YAML" "$STAGE_ID" <<'PY'
import sys

try:
    import yaml
except ModuleNotFoundError:
    sys.stderr.write("PyYAML is needed to read the release criteria\n")
    raise SystemExit(1)

path, stage = sys.argv[1], sys.argv[2]
with open(path) as handle:
    doc = yaml.safe_load(handle)
try:
    criteria = doc["stages"][stage]["criteria"]
except (KeyError, TypeError):
    sys.stderr.write(f"{path} defines no criteria for stage '{stage}'\n")
    raise SystemExit(1)
for criterion in criteria:
    print(f"{criterion['id']}|{criterion.get('severity', 'blocking')}")
PY
)

if [[ ${#SEVERITY[@]} -eq 0 ]]; then
    echo -e "${RED}cannot read $CRITERIA_YAML — it defines the severity of every check below.${NC}" >&2
    echo "  fix: $CRITERIA_PY -c 'import yaml'   # install PyYAML, or repair the file" >&2
    exit 3
fi

# record ID OUTCOME [DETAIL] [FIX] [waived]
#
# OUTCOME is `pass`, `fail`, or `not-measured`. `not-measured` is neither of the
# other two: the check did not run, so it proves nothing, and against a blocking
# criterion it stops the stage exactly as a failure does. Passing `waived` as the
# fifth argument downgrades one to a warning — for an EXPLICIT operator opt-out
# only, never for a check that merely could not run.
record() {
    local id="$1" outcome="$2" detail="${3:-}" fix="${4:-}" waived="${5:-}"
    local severity="${SEVERITY[$id]:-}"

    # The vocabulary is exactly three words. `warn` is NOT one of them: whether a
    # failure warns or blocks is the criteria file's decision, not the caller's,
    # and letting a caller say `warn` is how the two drifted apart before.
    case "$outcome" in
        pass|fail|not-measured) ;;
        *) echo -e "${RED}record: '$outcome' is not a valid outcome for '$id' (pass|fail|not-measured)${NC}" >&2
           exit 2 ;;
    esac

    if [[ -z "$severity" ]]; then
        echo -e "${RED}drift: '$id' is not a criterion of stage '$STAGE_ID' in $CRITERIA_YAML${NC}" >&2
        exit 2
    fi
    if [[ "$waived" == "waived" ]]; then severity=warn; fi
    RECORDED["$id"]=1

    local status
    case "$outcome" in
        pass) status=pass ;;
        *)    [[ "$severity" == "blocking" ]] && status=fail || status=warn ;;
    esac

    case "$outcome:$status" in
        pass:*)
            PASS=$((PASS+1)); echo -e "  ${GREEN}PASS${NC}  $id" >&2 ;;
        not-measured:warn)
            WARN=$((WARN+1))
            echo -e "  ${YELLOW}⊘ NOT MEASURED${NC}  $id  $detail (non-blocking)" >&2 ;;
        not-measured:fail)
            FAIL=$((FAIL+1))
            echo -e "  ${RED}⊘ NOT MEASURED${NC}  $id  $detail — proves nothing, not counted as a pass" >&2
            [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
        *:warn)
            WARN=$((WARN+1)); echo -e "  ${YELLOW}WARN${NC}  $id  $detail" >&2 ;;
        *)
            FAIL=$((FAIL+1)); echo -e "  ${RED}FAIL${NC}  $id  $detail" >&2
            [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
    esac

    RESULTS+=("$(printf '{"id":"%s","status":"%s","outcome":"%s","severity":"%s","detail":"%s","fix":"%s"}' \
        "$id" "$status" "$outcome" "$severity" "${detail//\"/\'}" "${fix//\"/\'}")")
}

echo "Preflight for ${VERSION:-<version from VERSION file>}" >&2

# ── Version consistency + CHANGELOG ────────────────────────────────────────
# ONE invocation, read as JSON, because check-version-consistency.py already
# owns BOTH criteria. In particular it defines `changelog-section` as a DATED
# `## [x.y.z] - YYYY-MM-DD` heading and treats it as warn-only in `ci` mode; a
# grep written here instead would be a second, looser implementation of a check
# that already exists — the exact drift the criteria table just lost.
#
# changelog-section is `warn` at this stage on purpose: preflight runs BEFORE
# `bump`, and bump is what promotes [Unreleased] into [X.Y.Z]. 70-tag.sh is the
# gate; this is the early notice, which the checker's suppressed stdout used to
# swallow entirely.
mapfile -t vc_lines < <(python3 - <<'PY' 2>/dev/null
import json
import subprocess
import sys

proc = subprocess.run(
    [sys.executable, "scripts/release/check-version-consistency.py", "--mode", "ci", "--json"],
    capture_output=True,
    text=True,
    check=False,
)
try:
    doc = json.loads(proc.stdout)
except ValueError:
    raise SystemExit(1)

changelog = next((c for c in doc["criteria"] if c["id"] == "changelog-section"), None)
print(doc["status"])
print(changelog["status"] if changelog else "absent")
print((changelog or {}).get("detail", "").replace("\n", " "))
PY
)

if [[ ${#vc_lines[@]} -lt 2 ]]; then
    record version-consistency fail \
        "check-version-consistency.py produced no readable JSON" \
        "python3 scripts/release/check-version-consistency.py --mode ci"
    record changelog-section not-measured \
        "the version checker, which owns this criterion, did not report" \
        "python3 scripts/release/check-version-consistency.py --mode ci"
else
    if [[ "${vc_lines[0]}" == "pass" ]]; then
        record version-consistency pass
    else
        record version-consistency fail \
            "version sources disagree" \
            "python3 scripts/release/check-version-consistency.py --mode ci"
    fi

    case "${vc_lines[1]}" in
        pass)
            record changelog-section pass ;;
        absent)
            record changelog-section not-measured \
                "the version checker no longer reports a changelog-section criterion" ;;
        *)
            record changelog-section fail \
                "${vc_lines[2]:-no dated section for this version} (bump promotes [Unreleased] into one)" \
                "write the section, or run the bump stage" ;;
    esac
fi

# ── Clean worktree ─────────────────────────────────────────────────────────
# A release must be reproducible from its tag. Uncommitted changes mean the
# images would contain code that the tag does not.
if [[ -z "$(git status --porcelain)" ]]; then
    record clean-worktree pass
else
    record clean-worktree fail \
        "$(git status --porcelain | wc -l) uncommitted change(s)" \
        "commit or stash before releasing"
fi

# ── Remote ARM64 builder ───────────────────────────────────────────────────
# Checked HERE rather than at publish time, which is where it used to be
# discovered — after the amd64 build had already run. The builder's docker
# context silently went stale when the Mac Studio's DHCP lease changed.
#
# A MISSING builder is a failure, not a warning. 80-publish.sh hard-exits
# without it ("cannot publish multi-arch"), and `promote` compares the digests
# of a manifest that lists both arches — so "single-arch builds only" is not a
# degraded release, it is no release. Recording it as `warn` moved the identical
# failure to hour three, which is the exact thing this check exists to prevent.
# Deliberately releasing without it is `--force-preflight "<reason>"`, recorded.
BUILDER="${REMOTE_BUILDER_NAME:-opentranscribe-multiarch}"
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    record remote-builder fail \
        "buildx builder '$BUILDER' not found — publish cannot produce a multi-arch manifest" \
        "./scripts/setup-remote-builder.sh setup"
elif docker buildx inspect "$BUILDER" 2>/dev/null | grep -qi 'error'; then
    endpoint="$(docker context inspect remote-arm64 --format '{{.Endpoints.docker.Host}}' 2>/dev/null || echo '?')"
    record remote-builder fail \
        "builder '$BUILDER' has a node in error (endpoint: $endpoint)" \
        "./scripts/setup-remote-builder.sh --host user@<current-ip>"
else
    record remote-builder pass
fi

# ── Scanners ───────────────────────────────────────────────────────────────
missing_tools=()
for tool in trivy grype syft; do
    command -v "$tool" >/dev/null 2>&1 || missing_tools+=("$tool")
done
if [[ ${#missing_tools[@]} -eq 0 ]]; then
    record security-tooling pass
else
    record security-tooling fail \
        "not installed: ${missing_tools[*]} (scan stage will skip those)" \
        "see scripts/security-scan.sh — it can self-install"
fi

# ── Release-test secrets ───────────────────────────────────────────────────
# Without a HuggingFace token the PyAnnote model cannot download and BOTH
# scenarios fail at their first transcription — hours in.
SECRETS="scripts/release-tests/.env.test-secrets"
if [[ -f "$SECRETS" ]] && grep -qE '^HUGGINGFACE_TOKEN=hf_' "$SECRETS"; then
    record release-test-secrets pass
else
    record release-test-secrets fail \
        "no HUGGINGFACE_TOKEN in $SECRETS (rehearse stage will fail at transcription)" \
        "cp $SECRETS.example $SECRETS && edit"
fi

# ── Release-test media ─────────────────────────────────────────────────────
# Both scenarios upload real media and assert the transcript is NON-EMPTY, so a
# missing or silent fixture fails the rehearsal for a reason unrelated to the
# release — after the stack is already up and an upload has been attempted.
TEST_MEDIA_DIR="${TEST_MEDIA_DIR:-/mnt/nvm/opentranscribe-test-runs/test-media}"
if [[ ! -d "$TEST_MEDIA_DIR" ]]; then
    record release-test-media fail \
        "no fixtures at $TEST_MEDIA_DIR (rehearse would fail at upload)" \
        "./scripts/release-tests/provision-test-media.sh"
else
    media_count=$(find "$TEST_MEDIA_DIR" -maxdepth 1 -type f \
        \( -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.mp4' -o -iname '*.wav' \
           -o -iname '*.flac' -o -iname '*.ogg' \) -size -5M 2>/dev/null | wc -l)
    if [[ "$media_count" -ge 1 ]]; then
        record release-test-media pass
    else
        record release-test-media fail \
            "$TEST_MEDIA_DIR has no media under 5 MB" \
            "./scripts/release-tests/provision-test-media.sh"
    fi
fi

# ── Disk ───────────────────────────────────────────────────────────────────
avail_gb=$(df -BG --output=avail /var/lib/docker 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
if [[ "${avail_gb:-0}" -ge 60 ]]; then
    record disk-space pass
else
    record disk-space fail "only ${avail_gb}GB free on the docker root (60GB+ recommended)"
fi

# ── Deployment matrix ──────────────────────────────────────────────────────
if ./scripts/validate-deployments.sh >/dev/null 2>&1; then
    record deployment-matrix pass
else
    record deployment-matrix fail \
        "a deployment permutation produces an invalid compose config" \
        "./scripts/validate-deployments.sh"
fi

# ── Live stack (informational here, blocking at `rehearse`) ────────────────
if docker ps --format '{{.Names}}' | grep -q '^opentranscribe-'; then
    record live-stack fail \
        "the live stack is running — the rehearse stage requires it stopped" \
        "./opentr.sh stop  (preserves all data)"
else
    record live-stack pass
fi

# ── The criteria file and this script must agree ───────────────────────────
# The other half of the contract. `record` rejects an id this stage does not
# define; this rejects a criterion this stage defines and never checked — which
# is how `docs-build` sat in preflight's list for months, implemented in verify,
# checked by neither.
unchecked=()
for id in "${!SEVERITY[@]}"; do
    [[ -n "${RECORDED[$id]:-}" ]] || unchecked+=("$id")
done
if [[ ${#unchecked[@]} -gt 0 ]]; then
    echo -e "${RED}drift: $CRITERIA_YAML defines these '$STAGE_ID' criteria, which this script never checked:${NC}" >&2
    printf '  %s\n' "${unchecked[@]}" >&2
    echo "  implement them here, or remove them from $CRITERIA_YAML" >&2
    exit 2
fi

# ── Report ─────────────────────────────────────────────────────────────────
if [[ "$JSON_OUT" == "true" ]]; then
    joined=$(IFS=,; echo "${RESULTS[*]}")
    next='["proceed to bump/verify"]'
    [[ $FAIL -gt 0 ]] && next='["fix the failing criteria above, then re-run preflight"]'
    printf '{"stage":"preflight","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "${VERSION:-unknown}" "$([[ $FAIL -eq 0 ]] && echo pass || echo fail)" "$joined" "$next"
fi

echo >&2
echo "—— $PASS passed, $FAIL failed, $WARN warnings" >&2
[[ $FAIL -eq 0 ]] || exit 1
exit 0
