#!/bin/bash
# Prove the CHECKED-OUT installer can install and update every PUBLISHED release.
#
# WHY THIS EXISTS (issue #683)
#
# A self-hosted install has two sources and only one of them is pinned:
#
#   setup-opentranscribe.sh  <- served from the DEFAULT BRANCH (the docs one-liner)
#   everything it downloads  <- served from the RESOLVED RELEASE TAG
#
# So the installer is always NEWER than the thing it installs. release-manifest.txt was
# added after v0.4.1 shipped; the new installer asked that tag for a file it had never
# heard of, 404ed, and fail-closed. Every `curl | bash` install died for 22 days and
# nothing noticed, because every test we had validated a tag against ITS OWN checkout.
#
# That is the blind spot this closes. The invariant is a CROSS-REF one and cannot be
# checked from a single tree:
#
#   The scripts at the tip must install every release that resolve_install_ref() can
#   resolve to — which means the newest published GitHub Release, and every older one
#   a user may pin with --version.
#
# HOW IT CHECKS
#
# It runs the REAL shell functions out of the REAL scripts against the REAL GitHub, in a
# throwaway directory. It does not re-implement the download loop; a re-implementation
# would have passed happily throughout the outage.
#
# Usage:
#   ./scripts/verify-install-paths.sh                 # the latest release (CI default)
#   ./scripts/verify-install-paths.sh --all-releases  # every published release
#   ./scripts/verify-install-paths.sh --ref v0.4.1    # one specific tag
#
# Env: GITHUB_TOKEN (optional) raises the API rate limit from 60/hr to 5000/hr.
# Exit: 0 all paths good, 1 a path is broken, 2 misuse//unreachable API.

set -euo pipefail

REPO="attevon-llc/OpenTranscribe"

# The oldest release `--version vX.Y.Z` is supported for, and WHY it is not simply "all".
#
# v0.1.0-v0.2.1 shipped before nginx/site.conf.template and scripts/generate-ssl-cert.sh
# existed, and both are non-optional in release-manifest.txt because the nginx/HTTPS
# deployment genuinely cannot run without them. The alternative -- marking them optional
# so old tags pass -- would stop the gate noticing if a CURRENT release lost them, which
# is the failure that actually matters. So the floor is declared, not papered over.
#
# Those releases were already 100% uninstallable before the #683 fix (they 404ed one step
# earlier, on the manifest itself), so nothing regressed here. Raise this only when a
# release genuinely drops out of support; never lower it to silence a red run.
MIN_SUPPORTED_RELEASE="v0.3.0"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/setup-opentranscribe.sh"
MANAGER="$ROOT/opentranscribe.sh"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'

FAILURES=0
CHECKS=0

UNSUPPORTED=0
# Set per-release: a failure below MIN_SUPPORTED_RELEASE is reported, but is not a gate
# failure. It must still be PRINTED -- an unsupported release that silently vanished from
# the output would be indistinguishable from one that passed.
LENIENT=false

pass() { CHECKS=$((CHECKS + 1)); echo "    ${GREEN}✓${NC} $1"; }
info() { echo "    ${BLUE}·${NC} $1"; }
fail() {
    CHECKS=$((CHECKS + 1))
    if [[ "$LENIENT" == true ]]; then
        UNSUPPORTED=$((UNSUPPORTED + 1))
        echo "    ${YELLOW}○${NC} $1 ${YELLOW}(below the v${MIN_SUPPORTED_RELEASE#v} support floor — not a gate failure)${NC}"
    else
        FAILURES=$((FAILURES + 1))
        echo "    ${RED}✗${NC} $1"
    fi
}

# True when $1 is older than MIN_SUPPORTED_RELEASE.
is_unsupported() {
    local oldest
    oldest=$(printf '%s\n%s\n' "${1#v}" "${MIN_SUPPORTED_RELEASE#v}" | sort -V | head -1)
    [[ "$oldest" == "${1#v}" && "${1#v}" != "${MIN_SUPPORTED_RELEASE#v}" ]]
}

gh_api() {
    local url="$1"
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        curl -fsSL --connect-timeout 10 --max-time 30 \
            -H "Authorization: Bearer ${GITHUB_TOKEN}" "$url"
    else
        curl -fsSL --connect-timeout 10 --max-time 30 "$url"
    fi
}

# Pull one shell function out of a script so it can be run in isolation. Same approach as
# backend/tests/unit/test_release_manifest.py::_extract_function -- deliberately, so the
# shell gate and the pytest gate exercise identical text.
# Two definition shapes exist and both must be extractable. opentranscribe.sh ships
# standalone, so read_env_value/resolve_default_branch are defined INSIDE an
# `if ! declare -F <name>; then ... fi` guard (common.sh's copy wins when present) --
# i.e. indented, and invisible to a `^name()` match. Extracting the whole guard block
# yields valid bash either way.
extract_fn() {
    local script="$1" name="$2" out
    out=$(sed -n "/^${name}()/,/^}/p" "$script")
    if [[ -z "$out" ]]; then
        out=$(sed -n "/^if ! declare -F ${name}\b/,/^fi$/p" "$script")
    fi
    printf '%s\n' "$out"
}

# Assert every function the checks below run actually exists, BEFORE running any of them.
#
# extract_fn is always called inside $(...), so an `exit` there would only kill the
# subshell: the harness would be built with an empty body and die with a bare 127, which
# reads like a broken test rather than a deleted invariant. Measured -- against a pre-fix
# tree this gate exited 127 with no usable message. Fail here instead, with the name.
require_functions() {
    local missing=() script name
    for spec in "$INSTALLER:resolve_install_ref" \
        "$INSTALLER:resolve_default_branch" \
        "$INSTALLER:download_release_manifest_artifacts" \
        "$MANAGER:read_env_value" \
        "$MANAGER:resolve_default_branch" \
        "$MANAGER:deployment_ref" \
        "$MANAGER:raw_url_for" \
        "$MANAGER:resolve_config_ref"; do
        script="${spec%%:*}"
        name="${spec##*:}"
        [[ -n "$(extract_fn "$script" "$name")" ]] || missing+=("$(basename "$script")::${name}()")
    done

    [[ ${#missing[@]} -eq 0 ]] && return 0

    echo "${RED}❌ Required function(s) missing — the install-path invariant is gone:${NC}" >&2
    printf '     %s\n' "${missing[@]}" >&2
    echo "   These are what make a pinned install resolvable (issue #683)." >&2
    echo "   Restore them, or update this gate deliberately if the design changed." >&2
    exit 1
}

# ---------------------------------------------------------------------------------------
# Path 1 — a FRESH INSTALL at $tag (`curl … | bash`, with or without --version)
#
# Runs the installer's real download loop. Then checks the thing the 404 hid: that the
# files on disk are the TAG's bytes, not the default branch's. A manifest fallback that
# quietly pulled artifacts from the tip would "pass" an existence check while shipping an
# unpinned install -- the exact bug pinning exists to prevent.
# ---------------------------------------------------------------------------------------
check_fresh_install() {
    local tag="$1" workdir log
    workdir=$(mktemp -d)
    log=$(mktemp)

    if ! (
        cd "$workdir"
        # shellcheck disable=SC2016  # the harness is built as text on purpose
        bash -c "
            set -uo pipefail
            RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
            $(extract_fn "$INSTALLER" resolve_default_branch)
            $(extract_fn "$INSTALLER" download_release_manifest_artifacts)
            OPENTRANSCRIBE_BRANCH='$tag' download_release_manifest_artifacts
        "
    ) >"$log" 2>&1; then
        fail "fresh install at ${tag}: the download loop failed"
        sed 's/^/        /' "$log" | tail -15
        rm -rf "$workdir" "$log"
        return
    fi
    pass "fresh install at ${tag}: all required artifacts downloaded"

    if grep -q "predates release-manifest.txt" "$log"; then
        info "used the fallback manifest (this release predates release-manifest.txt)"
    fi

    # Every non-optional manifest entry must be on disk.
    local missing=() path flags
    while IFS= read -r line; do
        case "$line" in '' | '#'*) continue ;; esac
        path=$(printf '%s' "$line" | cut -f1 | tr -d '[:space:]')
        flags=$(printf '%s' "$line" | cut -s -f2)
        [[ -n "$path" ]] || continue
        case ",$flags," in *,optional,*) continue ;; esac
        [[ -f "$workdir/$path" ]] || missing+=("$path")
    done <"$workdir/release-manifest.txt"

    if [[ ${#missing[@]} -eq 0 ]]; then
        pass "fresh install at ${tag}: every required manifest path is on disk"
    else
        fail "fresh install at ${tag}: required paths absent: ${missing[*]}"
    fi

    # THE pin check: bytes must match the tag, not the tip.
    local drifted=() f tag_sha got_sha
    for f in docker-compose.yml opentranscribe.sh .env.example; do
        [[ -f "$workdir/$f" ]] || continue
        tag_sha=$(git -C "$ROOT" show "${tag}:${f}" 2>/dev/null | sha256sum | cut -d' ' -f1)
        got_sha=$(sha256sum "$workdir/$f" | cut -d' ' -f1)
        [[ -n "$tag_sha" ]] || continue
        [[ "$tag_sha" == "$got_sha" ]] || drifted+=("$f")
    done

    if [[ ${#drifted[@]} -eq 0 ]]; then
        pass "fresh install at ${tag}: artifacts are ${tag} content (pin intact)"
    else
        fail "fresh install at ${tag}: NOT ${tag} content -- install is unpinned: ${drifted[*]}"
    fi

    rm -rf "$workdir" "$log"
}

# ---------------------------------------------------------------------------------------
# Path 2 — `opentranscribe.sh update-full` on an install pinned to $tag
#
# The failure mode here is silent, not a 404: config from the tip merged onto pinned
# images. So the assertion is on the REF CHOSEN, not on whether a download succeeded.
# ---------------------------------------------------------------------------------------
check_update_full_ref() {
    local tag="$1" workdir got
    workdir=$(mktemp -d)
    printf 'OT_IMAGE_TAG=%s\n' "$tag" >"$workdir/.env"

    got=$(cd "$workdir" && bash -c "
        set -uo pipefail
        RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
        $(extract_fn "$MANAGER" read_env_value)
        $(extract_fn "$MANAGER" resolve_default_branch)
        $(extract_fn "$MANAGER" deployment_ref)
        $(extract_fn "$MANAGER" resolve_config_ref)
        unset OPENTRANSCRIBE_BRANCH
        resolve_config_ref
    " 2>/dev/null)

    if [[ "$got" == "$tag" ]]; then
        pass "update-full on a ${tag} install: takes config from ${tag}"
    else
        fail "update-full on a ${tag} install: took config from '${got}' -- config/image mismatch"
    fi
    rm -rf "$workdir"
}

# ---------------------------------------------------------------------------------------
# Path 3 — an UNPINNED install (pre-pinning, OT_IMAGE_TAG unset or 'latest')
#
# Backward compatibility: these installs predate OT_IMAGE_TAG entirely and must keep
# working -- but they must SAY they are unpinned rather than pretending otherwise.
# ---------------------------------------------------------------------------------------
check_unpinned_update_full() {
    local label="$1" env_body="$2" workdir out rc
    workdir=$(mktemp -d)
    printf '%s' "$env_body" >"$workdir/.env"

    set +e
    out=$(cd "$workdir" && bash -c "
        set -uo pipefail
        RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
        $(extract_fn "$MANAGER" read_env_value)
        $(extract_fn "$MANAGER" resolve_default_branch)
        $(extract_fn "$MANAGER" deployment_ref)
        $(extract_fn "$MANAGER" resolve_config_ref)
        unset OPENTRANSCRIBE_BRANCH
        resolve_config_ref
    " 2>&1)
    rc=$?
    set -e

    if [[ $rc -eq 0 ]] && grep -q "not pinned to a release" <<<"$out"; then
        pass "update-full on ${label}: still works AND warns it is unpinned"
    elif [[ $rc -eq 0 ]]; then
        fail "update-full on ${label}: took tip config with NO unpinned warning"
    else
        fail "update-full on ${label}: refused outright (rc=${rc}) -- breaks existing installs"
    fi
    rm -rf "$workdir"
}

# ---------------------------------------------------------------------------------------
# Path 4 — the default `curl | bash` resolves to the newest PUBLISHED release
#
# resolve_install_ref() reads the GitHub *Release*, never the newest git tag: a tag
# appears 30-90 min before its images are promoted, and resolving to it would hand new
# users a version whose images do not exist. This asserts that is still what happens.
# ---------------------------------------------------------------------------------------
check_default_install_resolves_latest() {
    local expected="$1" got
    got=$(bash -c "
        set -uo pipefail
        print_info(){ :; }; print_success(){ :; }; print_error(){ :; }; print_warning(){ :; }
        $(extract_fn "$INSTALLER" resolve_install_ref)
        unset OPENTRANSCRIBE_VERSION OPENTRANSCRIBE_BRANCH
        resolve_install_ref >/dev/null 2>&1
        printf '%s|%s\n' \"\$OPENTRANSCRIBE_BRANCH\" \"\$OT_IMAGE_TAG\"
    " 2>/dev/null || true)

    if [[ "$got" == "${expected}|${expected}" ]]; then
        pass "default 'curl | bash' resolves to ${expected} and pins images to it"
    else
        fail "default 'curl | bash' resolved '${got}', expected '${expected}|${expected}'"
    fi
}

main() {
    local mode=latest explicit_ref=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --all-releases) mode=all; shift ;;
            --ref) explicit_ref="${2:-}"; mode=one
                  [[ -n "$explicit_ref" ]] || { echo "--ref needs a tag" >&2; exit 2; }
                  shift 2 ;;
            -h | --help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
            *) echo "unknown argument: $1" >&2; exit 2 ;;
        esac
    done

    echo "${BLUE}▶ Verifying install/update paths against real published releases${NC}"
    echo "  repo: ${REPO}   scripts: working tree at $(git -C "$ROOT" rev-parse --short HEAD)"
    echo ""

    require_functions

    local latest default_branch
    if ! latest=$(gh_api "https://api.github.com/repos/${REPO}/releases/latest" |
        grep -m1 '"tag_name"' | sed -E 's/.*"(v?[0-9]+\.[0-9]+\.[0-9]+)".*/\1/'); then
        echo "${RED}✗ Could not reach the GitHub API (rate limited? set GITHUB_TOKEN)${NC}" >&2
        exit 2
    fi
    default_branch=$(gh_api "https://api.github.com/repos/${REPO}" |
        grep -m1 '"default_branch"' | sed -E 's/.*"default_branch"[^"]*"([^"]+)".*/\1/')

    echo "  latest published release: ${YELLOW}${latest}${NC}"
    echo "  default branch (installer is served from here): ${YELLOW}${default_branch}${NC}"
    echo ""

    local -a targets=()
    case "$mode" in
        latest) targets=("$latest") ;;
        one) targets=("$explicit_ref") ;;
        all)
            mapfile -t targets < <(gh_api "https://api.github.com/repos/${REPO}/releases" |
                grep '"tag_name"' | sed -E 's/.*"(v?[0-9]+\.[0-9]+\.[0-9]+)".*/\1/')
            ;;
    esac

    echo "${BLUE}▶ Path 4: default one-line install${NC}"
    check_default_install_resolves_latest "$latest"
    echo ""

    echo "${BLUE}▶ Path 3: pre-pinning installs (backward compatibility)${NC}"
    check_unpinned_update_full "an install with no OT_IMAGE_TAG" 'FOO=bar
'
    check_unpinned_update_full "an install pinned to 'latest'" 'OT_IMAGE_TAG=latest
'
    echo ""

    local tag
    for tag in "${targets[@]}"; do
        if is_unsupported "$tag"; then
            LENIENT=true
            echo "${BLUE}▶ Release ${tag}${NC} ${YELLOW}(pre-${MIN_SUPPORTED_RELEASE}, unsupported)${NC}"
        else
            LENIENT=false
            echo "${BLUE}▶ Release ${tag}${NC}"
        fi
        check_fresh_install "$tag"
        check_update_full_ref "$tag"
        echo ""
    done

    echo "─────────────────────────────────────────────────────"
    if [[ $UNSUPPORTED -gt 0 ]]; then
        echo "${YELLOW}○ ${UNSUPPORTED} check(s) failed on releases older than ${MIN_SUPPORTED_RELEASE}${NC}"
        echo "  Those tags predate files release-manifest.txt marks required; they were"
        echo "  uninstallable before this gate existed too. Not counted as failures."
    fi
    if [[ $FAILURES -eq 0 ]]; then
        echo "${GREEN}✅ ${CHECKS} checks run, 0 failures — every supported path installs and updates cleanly${NC}"
        exit 0
    fi
    echo "${RED}❌ ${FAILURES} of ${CHECKS} checks FAILED${NC}"
    echo "   A user running the documented one-liner would hit this. Do not release."
    exit 1
}

main "$@"
