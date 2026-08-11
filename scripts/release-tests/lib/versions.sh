#!/bin/bash
# Version resolution for the release harness and the release orchestrator.
#
# Two rules this file exists to enforce:
#
# 1. vX.Y.Z is canonical. Docker Hub tags are v-prefixed (docker-build-push.sh
#    force-prepends the v), so every version that crosses a boundary carries it.
#    The two scenarios used to disagree — test-fresh-install.sh defaulted to
#    "v0.4.0" and test-upgrade-from-v033.sh to "0.4.0" — so the upgrade
#    scenario's default never matched a real local build.
#
# 2. The previous release is DISCOVERED, never read from a checked-in file.
#    GitLab's Gitaly deleted its backwards-compatibility CI job precisely
#    because it read the prior version from a VERSION file that went stale and
#    silently tested the wrong release. Our own expected-schemas.tsv rotted the
#    same way (it never got a v0.4.1 row, and nothing read it). Git tags say
#    what was released; Docker Hub says what is actually installable. We
#    require both.
#
# Sourcing contract: requires REPO_ROOT. Optional TEST_ROOT enables caching of
# Docker Hub lookups. Uses gr_* loggers when guardrails.sh is already sourced,
# otherwise falls back to plain echo so this file is usable standalone.

set -euo pipefail

: "${REPO_ROOT:?versions.sh requires REPO_ROOT}"
: "${DOCKERHUB_USERNAME:=davidamacey}"

# Standalone-safe logging (guardrails.sh defines these when present).
if ! declare -F gr_log >/dev/null 2>&1; then
    gr_log()  { echo "[versions] $*"; }
    gr_ok()   { echo "[versions] ✓ $*"; }
    gr_warn() { echo "[versions] ⚠ $*" >&2; }
    gr_die()  { echo "[versions] ✗ FATAL: $*" >&2; exit 1; }
fi

# ---------------------------------------------------------------- normalisation

# vX.Y.Z, whatever went in.
ver_normalize() {
    local v="${1#v}"
    [[ -n "$v" ]] || return 1
    echo "v${v}"
}

# X.Y.Z (no prefix) — for pyproject.toml / package.json comparisons.
ver_semver() {
    echo "${1#v}"
}

ver_is_valid() {
    [[ "${1#v}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Strict less-than on semver. Uses sort -V so v0.10.0 > v0.9.0 (a plain string
# compare gets this backwards, and we will reach 0.10 before long).
ver_lt() {
    local a="${1#v}" b="${2#v}"
    [[ "$a" != "$b" ]] && [[ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | head -1)" == "$a" ]]
}

ver_lte() {
    local a="${1#v}" b="${2#v}"
    [[ "$a" == "$b" ]] || ver_lt "$a" "$b"
}

# ------------------------------------------------------------------- the target

# The release under test. This can only come from the VERSION file: when the
# release tests run, the new git tag does not exist yet and the new images are
# not on Docker Hub.
ver_to_version() {
    if [[ -n "${TO_VERSION:-}" ]]; then
        ver_normalize "$TO_VERSION"
        return
    fi
    local raw
    raw="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
    [[ -n "$raw" ]] || gr_die "VERSION file is empty"
    ver_is_valid "$raw" || gr_die "VERSION file holds '$raw', expected vX.Y.Z"
    ver_normalize "$raw"
}

# ------------------------------------------------------------------ docker hub

_ver_hub_cache() {
    echo "${TEST_ROOT:-${TMPDIR:-/tmp}}/.hub-tags"
}

# ver_hub_has <repo-suffix> <tag> — is davidamacey/opentranscribe-<suffix>:<tag>
# published? Memoized: `docker manifest inspect` counts against the anonymous
# 100-pulls-per-6h limit, and previous-version detection probes several tags.
ver_hub_has() {
    local suffix="$1" tag="$2"
    local image="${DOCKERHUB_USERNAME}/opentranscribe-${suffix}:${tag}"
    local cache key
    cache="$(_ver_hub_cache)"
    key="${image}"

    if [[ -f "$cache" ]]; then
        local cached
        cached="$(grep -F "$key " "$cache" 2>/dev/null | tail -1 | awk '{print $2}')" || true
        if [[ -n "$cached" ]]; then
            [[ "$cached" == "yes" ]]
            return
        fi
    fi

    local result="no"
    if docker manifest inspect "$image" >/dev/null 2>&1; then
        result="yes"
    fi
    mkdir -p "$(dirname "$cache")" 2>/dev/null || true
    printf '%s %s\n' "$key" "$result" >> "$cache" 2>/dev/null || true
    [[ "$result" == "yes" ]]
}

# Both the images a deployment actually needs must exist, not just one.
ver_hub_has_release() {
    local tag="$1"
    ver_hub_has backend "$tag" && ver_hub_has frontend "$tag"
}

# ---------------------------------------------------------------- the previous

# Candidate release tags, newest first, excluding pre-releases.
ver_release_tags() {
    git -C "$REPO_ROOT" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname \
        | grep -Ev -- '-(rc|beta|alpha|dev)' || true
}

# The release we upgrade FROM. Git tags are the candidate list; Docker Hub is the
# filter. A tag can exist in git while its images were never pushed — that tag is
# not something a real user could be running, so it is not a valid upgrade source.
#
# Honours FROM_VERSION as an explicit override (still Hub-verified).
# Prints nothing and returns 1 when there is no valid previous release, which is
# the legitimate first-release case; callers decide whether that is fatal
# (REQUIRE_PREVIOUS=1) or a skip.
ver_previous_version() {
    local to
    to="$(ver_to_version)"

    if [[ -n "${FROM_VERSION:-}" ]]; then
        local pinned
        pinned="$(ver_normalize "$FROM_VERSION")"
        if ! ver_hub_has_release "$pinned"; then
            gr_die "FROM_VERSION=$pinned has no published backend+frontend images on Docker Hub"
        fi
        echo "$pinned"
        return 0
    fi

    local tag
    while read -r tag; do
        [[ -n "$tag" ]] || continue
        ver_is_valid "$tag" || continue
        # Strictly older than the target, semver-wise.
        ver_lt "$tag" "$to" || continue
        if ver_hub_has_release "$tag"; then
            echo "$tag"
            return 0
        fi
        gr_warn "skipping $tag: tagged in git but not published to Docker Hub"
    done < <(ver_release_tags)

    return 1
}

# Advisory cross-check: a tag that exists locally but was never pushed as a
# GitHub Release is a strong hint someone tagged and did not finish. Warning
# only, and only when gh is available — gh must never be a hard dependency.
ver_warn_if_unreleased() {
    local tag="$1"
    command -v gh >/dev/null 2>&1 || return 0
    local found
    found="$(gh release list --limit 50 --json tagName --jq '.[].tagName' 2>/dev/null || true)"
    [[ -n "$found" ]] || return 0
    if ! grep -qx -- "$tag" <<< "$found"; then
        gr_warn "$tag has Docker Hub images but no GitHub Release — was a release abandoned?"
    fi
}

# ---------------------------------------------------------------------- alembic

# The single head of a migration chain, derived from the down_revision graph.
# Accepts any backend dir, including one inside a worktree checked out at an old
# release tag — which is how we learn what head the FROM release shipped with,
# without maintaining a table of it.
# shellcheck disable=SC2120  # optional arg; callers outside this file pass a worktree
ver_alembic_head() {
    local backend_dir="${1:-$REPO_ROOT/backend}"
    python3 "$(dirname "${BASH_SOURCE[0]}")/alembic-head.py" "$backend_dir"
}

# ------------------------------------------------------------------- reporting

ver_summary() {
    local to from head
    to="$(ver_to_version)"
    head="$(ver_alembic_head 2>/dev/null || echo '<unresolved>')"
    if from="$(ver_previous_version)"; then
        :
    else
        from="<none published>"
    fi
    echo "  target version (VERSION):  $to"
    echo "  previous published:        $from"
    echo "  alembic head (derived):    $head"
}
