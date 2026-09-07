#!/bin/bash
# Patch/hotfix release predicates — the ONE place "is this a patch, and is its
# rehearsal waivable" gets decided (issue #784).
#
# Three consumers care about this fact: release.sh (must refuse --patch on a
# non-patch delta), 65-rehearse.sh (must know whether it was waived, to record
# the ledger detail correctly), and 90-promote.sh (must know the newest
# published release to guard :latest against moving backwards on a hotfix cut
# from an old release branch). Three independent implementations of "is this a
# patch" is three chances for them to disagree — the exact drift
# scripts/release/criteria-lib.sh exists to prevent for the pass/fail contract,
# applied here to the version-delta question instead.
#
# There is deliberately no flag anywhere that DECLARES the release is a patch.
# releasing.md's "version facts are derived, never recorded" rule applies here
# too: a declared type is a fact that can be wrong exactly when it matters —
# at the moment an operator is deciding whether it is safe to skip evidence.
#
# Sourcing contract: requires REPO_ROOT (same contract as versions.sh, which
# this sources).
#
# ⚠️ versions.sh unconditionally runs `set -euo pipefail`. release.sh already
# runs under -e, but 70-tag.sh / 65-rehearse.sh / 90-promote.sh deliberately do
# NOT (see 70-tag.sh's header on issue #784 finding N4 — that omission is what
# let an unchecked `git push` fall through to a recorded pass). Sourcing this
# file must not flip -e on for a caller that deliberately left it off, so the
# caller's own -e state is restored immediately after the versions.sh source.

PATCH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_patch_lib_had_e="$-"
# shellcheck source=scripts/release-tests/lib/versions.sh
source "$PATCH_LIB_DIR/../release-tests/lib/versions.sh"
case "$_patch_lib_had_e" in
    *e*) ;;                 # caller already had -e — leave it on
    *)   set +e ;;          # caller deliberately did not — versions.sh must not turn it on
esac
unset _patch_lib_had_e

# patch_base_tag TO -> the highest valid non-prerelease tag strictly below TO,
# on stdout. Returns 1 with nothing printed when there is none.
#
# ⚠️ Deliberately NOT ver_previous_version(): that additionally requires Docker
# Hub to carry both images, which is the right question for an upgrade
# rehearsal ("what can a real user upgrade FROM") and the wrong one here
# ("what changed since the last tag" is a question about git, not about what
# got published).
patch_base_tag() {
    local to; to="$(ver_normalize "$1")" || return 1
    local tag
    while read -r tag; do
        [[ -n "$tag" ]] || continue
        ver_is_valid "$tag" || continue
        if ver_lt "$tag" "$to"; then
            echo "$tag"
            return 0
        fi
    done < <(ver_release_tags)
    return 1
}

# patch_release_kind TO [BASE] -> patch|minor|major|unknown, on stdout. Always
# prints something and returns 0 — "unknown" is itself the answer when there is
# no base tag or the base is not a valid predecessor, never a bash error exit.
#
# Derived from the numeric delta against BASE (or patch_base_tag TO, computed
# once and reused by patch_rehearsal_waivable so callers never resolve it
# twice and risk two different answers within one invocation).
patch_release_kind() {
    local to_v; to_v="$(ver_normalize "$1")" || { echo unknown; return 0; }
    local base="${2:-}"
    if [[ -z "$base" ]]; then
        base="$(patch_base_tag "$to_v")" || { echo unknown; return 0; }
    fi
    base="$(ver_normalize "$base")" || { echo unknown; return 0; }

    local to; to="$(ver_semver "$to_v")"
    base="$(ver_semver "$base")"
    ver_is_valid "$to" || { echo unknown; return 0; }
    ver_is_valid "$base" || { echo unknown; return 0; }

    local to_major to_minor to_patch base_major base_minor base_patch
    IFS='.' read -r to_major to_minor to_patch <<< "$to"
    IFS='.' read -r base_major base_minor base_patch <<< "$base"

    if [[ "$to_major" != "$base_major" ]]; then
        echo major
    elif [[ "$to_minor" != "$base_minor" ]]; then
        echo minor
    elif [[ "$to_patch" != "$base_patch" ]]; then
        echo patch
    else
        echo unknown
    fi
}

# PATCH_REHEARSAL_TRIGGERS: any of these paths/substrings appearing in
# `git diff --name-only BASE..HEAD` means the rehearsal must NOT be waived —
# full evidence, not a mechanical shortcut. This is the WIDENED set (owner's
# explicit choice, #784): the issue's own text names only
# backend/alembic/versions/** and a Dockerfile diff, and the entries below
# extend that. Every entry needs a written reason, same convention as
# backend/tests/audit-allowlist.txt's mandatory-reason entries — a `grep -F`
# that matches nothing here would waive EVERY rehearsal, silently.
declare -a PATCH_REHEARSAL_TRIGGERS=(
    # a migration is neither skippable nor revertible-by-pulling-the-previous-
    # image (roadmap.md's patch-release rule); the rollback tail in
    # test-upgrade.sh is the only thing that proves it actually is either.
    'backend/alembic/versions/'
    # changes what is IN the image; fresh-install and lite are the only things
    # in this pipeline that ever run it.
    'Dockerfile'
    # a pin bump changes the image with NO Dockerfile diff — the nltk pathsec
    # break (scripts/CLAUDE.md) shipped exactly this way.
    'requirements'
    # same failure shape, frontend side.
    'package-lock.json'
    # changes deployment SHAPE, which all three rehearsal scenarios exercise.
    'docker-compose'
    # the install path itself.
    'setup-opentranscribe.sh'
    # the shipped front end the upgrade scenario drives end to end.
    'opentranscribe.sh'
    # issue #683 — a manifest change broke every `curl | bash` install for 22
    # days; nothing else in this trigger set would have caught that class of
    # regression.
    'release-manifest.txt'
)

# patch_rehearsal_waivable TO -> 0 + a reason on stdout when the rehearsal MAY
# be skipped; 1 + why on stderr otherwise. Every refusal path is deliberate:
#   - TO is not a patch relative to its base -> not this mechanism's job.
#   - no base tag to diff against -> nothing to compare, so nothing is waivable.
#   - the base..HEAD diff is EMPTY -> "refusing to waive on no evidence": an
#     empty diff almost always means the derivation itself failed (wrong base,
#     wrong ref), never that a real patch changed literally nothing.
#   - `git diff` itself fails -> same refusal, for the same reason.
#   - the diff touches any PATCH_REHEARSAL_TRIGGERS entry -> full evidence.
patch_rehearsal_waivable() {
    local to; to="$(ver_normalize "$1")" || { echo "invalid version: $1" >&2; return 1; }

    local base
    base="$(patch_base_tag "$to")" || {
        echo "no base tag to diff against" >&2
        return 1
    }

    local kind; kind="$(patch_release_kind "$to" "$base")"
    if [[ "$kind" != "patch" ]]; then
        echo "$to is a '$kind' release relative to $base, not a patch" >&2
        return 1
    fi

    local diff_files
    if ! diff_files="$(git -C "$REPO_ROOT" diff --name-only "${base}..HEAD" 2>&1)"; then
        echo "git diff ${base}..HEAD failed: $diff_files" >&2
        return 1
    fi
    if [[ -z "$diff_files" ]]; then
        echo "refusing to waive on no evidence — ${base}..HEAD diffed empty" >&2
        return 1
    fi

    local trigger
    for trigger in "${PATCH_REHEARSAL_TRIGGERS[@]}"; do
        if grep -qF -- "$trigger" <<< "$diff_files"; then
            echo "diff touches '$trigger' — full rehearsal required" >&2
            return 1
        fi
    done

    echo "patch $to vs $base touches none of ${#PATCH_REHEARSAL_TRIGGERS[@]} disqualifying triggers"
    return 0
}

# newest_published_release -> the newest git tag (by ver_release_tags' order)
# that has BOTH backend and frontend images on Docker Hub, on stdout. Returns 1
# with nothing printed when none resolves (e.g. Docker Hub unreachable) — the
# caller must treat that as "I don't know", never as "nothing is published".
#
# Same two authorities versions.sh already treats as authoritative for exactly
# this kind of question: git tags as the candidate list (ver_release_tags),
# Docker Hub as the filter (ver_hub_has_release).
newest_published_release() {
    local tag
    while read -r tag; do
        [[ -n "$tag" ]] || continue
        ver_is_valid "$tag" || continue
        if ver_hub_has_release "$tag"; then
            echo "$tag"
            return 0
        fi
    done < <(ver_release_tags)
    return 1
}
