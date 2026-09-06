#!/bin/bash
# Move :latest to the validated digest. A COPY, never a rebuild.
#
# `buildx imagetools create` copies the manifest, so :latest and :vX.Y.Z resolve
# to the SAME digest. Rebuilding for :latest would produce a second artifact that
# nothing validated and that can differ from the one that was tested.
#
# Exit: 0 promoted · 1 failed or digests disagree

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

VERSION="${1:-${RELEASE_VERSION:-}}"
JSON_OUT="${JSON_OUT:-false}"
# No USER_NS here any more: the repo namespace comes from security-scan.sh list-repos, which
# reads DOCKERHUB_USERNAME itself, so the two cannot resolve to different namespaces.
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
: "${VERSION:?90-promote.sh needs a version}"

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=promote
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

digest_of() {
    docker buildx imagetools inspect "$1" --format '{{println .Manifest.Digest}}' 2>/dev/null | head -1
}

# Derived from security-scan.sh's component table, never listed here. This loop used to be a
# literal `for repo in backend backend-lite frontend docs`, which disagreed with 95-finish.sh's
# literal `backend frontend` — see scripts/release/published-repos.sh.
# shellcheck source=scripts/release/published-repos.sh
source "$SCRIPT_DIR/published-repos.sh"

# Captured into a variable, NOT piped in via `< <(...)` — see the note in 95-finish.sh: the
# helper's `exit 3` inside a process substitution would end only that subshell, leaving this
# loop to iterate zero times and report success.
list_rc=0
repos_tsv="$(release_published_repos_or_die)" || list_rc=$?
if (( list_rc != 0 )); then
    record published-repo-list-derived not-measured \
        "security-scan.sh list-repos yielded nothing (rc=$list_rc)" \
        "./scripts/security-scan.sh list-repos"
    # Exit code unchanged: still the helper's own rc (3 = precondition unmet). The criteria
    # are emitted first so a --json consumer learns WHY rather than just seeing a 3.
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"promote","version":"%s","status":"fail","criteria":[%s],"next":["./scripts/security-scan.sh list-repos"]}\n' \
            "$VERSION" "$(criteria_json)"
    fi
    exit "$list_rc"
fi
record published-repo-list-derived pass

# ─────────────────────────────────── :latest regression guard (issue #784) ──
#
# Not a hazard #784's own issue text names — one it would otherwise CREATE. A
# hotfix cut from an old release/<minor> branch, after a NEWER minor has
# already published, would on the standard pipeline move :latest BACKWARDS —
# silently downgrading every existing user on their next pull. `imagetools
# create` is a manifest copy, not a rebuild, and it is not undoable, so this
# runs before the loop touches anything.
#
# shellcheck source=scripts/release/patch-lib.sh
source "$SCRIPT_DIR/patch-lib.sh"

newest="$(newest_published_release)" || {
    record latest-target-determined fail \
        "could not resolve the newest published release from git tags x Docker Hub" \
        "check network access / DOCKERHUB_USERNAME, then re-run promote"
    # Not-measured, deliberately: the check below never ran either, so nothing
    # about it is known — "I don't know whether this is a downgrade" is not a
    # licence to move a tag every user pulls. Precondition (exit 3), not a
    # gate that ran and found a real regression.
    record latest-not-regressed not-measured "the newest published release is unknown"
    echo -e "${RED}cannot determine the newest published release — refusing to move :latest blind${NC}" >&2
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"promote","version":"%s","status":"fail","criteria":[%s],"next":["resolve Docker Hub / git-tag access, then re-run promote"]}\n' \
            "$VERSION" "$(criteria_json)"
    fi
    exit 3
}
record latest-target-determined pass "newest published release: $newest"

IS_BACKPORT=false
if ver_lt "$VERSION" "$newest"; then
    IS_BACKPORT=true
    echo -e "${YELLOW}${VERSION} is older than the newest published release (${newest}) — this is a backport${NC}" >&2
    echo -e "${YELLOW}  :latest will be left pointing at ${newest}; that is a PASS, not a failure${NC}" >&2
    record latest-not-regressed pass \
        "$VERSION < $newest — :latest is correctly left alone (backport, not a regression)"
else
    record latest-not-regressed pass "$VERSION >= $newest — promoting will not move :latest backwards"
fi

fail=0
repos_seen=0
unpublished=()      # repos with no :$VERSION on the Hub at all
copy_failed=()      # repos where imagetools create itself failed
digest_mismatch=()  # repos where :latest and :$VERSION disagree afterwards
while IFS=$'\t' read -r component img; do
    [[ -n "$img" ]] || continue
    repo="${img##*/}"
    repos_seen=$((repos_seen + 1))
    # A missing :$VERSION is a FAILURE, not a SKIP. Promote's whole job is moving :latest onto
    # the validated digest; a repo with nothing to promote means the publish stage did not do
    # what the pipeline believes it did, and skipping it silently let :latest keep pointing at
    # the PREVIOUS release for that component while the release completed green. That is how an
    # unpublished lite image could ride all the way through to `finish` unnoticed.
    docker manifest inspect "${img}:${VERSION}" >/dev/null 2>&1 || {
        echo -e "${RED}FAIL  ${component}: no ${img}:${VERSION} published — cannot promote${NC}" >&2
        unpublished+=("$repo"); fail=1; continue; }

    # version-tag-published is still asserted on the backport path (we just proved the tag
    # exists, above) — only the :latest MOVE is skipped. This is the load-bearing guard: it
    # fires even when an operator invokes `./scripts/release.sh promote vX.Y.Z --yes` directly,
    # bypassing release.sh's own --patch bookkeeping entirely.
    if [[ "$IS_BACKPORT" == "true" ]]; then
        echo -e "${YELLOW}SKIP  ${repo}: ${VERSION} is a backport older than :latest (${newest}) — not moving :latest${NC}" >&2
        continue
    fi

    echo -e "${YELLOW}promoting ${img}:${VERSION} -> :latest${NC}" >&2
    docker buildx imagetools create -t "${img}:latest" "${img}:${VERSION}" || {
        copy_failed+=("$repo"); fail=1; continue; }

    src=$(digest_of "${img}:${VERSION}"); dst=$(digest_of "${img}:latest")
    if [[ -n "$src" && "$src" == "$dst" ]]; then
        echo -e "${GREEN}PASS  ${repo}: :latest and :${VERSION} are the same digest${NC}" >&2
    else
        echo -e "${RED}FAIL  ${repo}: digests differ (${src:0:19} vs ${dst:0:19})${NC}" >&2
        digest_mismatch+=("$repo"); fail=1
    fi
done <<< "$repos_tsv"

# Zero repos iterated is COULD NOT CHECK, never "everything promoted" — the same rule the
# scan stage applies to an empty leg list. `release_published_repos_or_die` already refuses an
# empty list, so reaching here with 0 means the TSV was non-empty but every line was blank.
if (( repos_seen == 0 )); then
    record version-tag-published not-measured "no repo lines to check"
    record latest-copied not-measured "no repo lines to copy"
    record latest-digest-matches-version not-measured "no digests to compare"
    fail=1
else
    if (( ${#unpublished[@]} )); then
        record version-tag-published fail "no :${VERSION} published for: ${unpublished[*]}" \
            "./scripts/release.sh publish $VERSION --yes"
    else
        record version-tag-published pass
    fi

    # A repo whose :$VERSION was missing was never offered to imagetools, so it is not
    # evidence either way about the copy — reporting it as a copy failure would blame the
    # wrong step. Only a create that actually ran and failed counts.
    #
    # IS_BACKPORT is checked FIRST, in EACH chain independently (not merged into one): with
    # IS_BACKPORT=true, copy_failed/digest_mismatch are always empty (the loop `continue`s
    # before attempting either), so falling through to the pre-existing `else` would record a
    # PASS for a copy and a digest comparison that never ran — the "criterion recorded while
    # nothing happened" shape #784 exists to prevent, inverted (a false pass, not a false
    # clean scan). Keeping the two chains independent (rather than one merged if/elif) matters
    # because a mixed run — one repo's copy failing while another repo's digest mismatches —
    # must still report BOTH failures; a single merged chain would let a copy_failed entry
    # mask an unrelated digest_mismatch on a different repo.
    if [[ "$IS_BACKPORT" == "true" ]]; then
        record latest-copied not-measured \
            "${VERSION} is a backport older than :latest (${newest}) — imagetools create intentionally not run" \
            "" waived
    elif (( ${#copy_failed[@]} )); then
        record latest-copied fail "imagetools create failed for: ${copy_failed[*]}"
    elif (( ${#unpublished[@]} == repos_seen )); then
        record latest-copied not-measured "nothing was published, so no copy was attempted"
    else
        record latest-copied pass
    fi

    if [[ "$IS_BACKPORT" == "true" ]]; then
        record latest-digest-matches-version not-measured \
            "${VERSION} is a backport — :latest was not touched, so there is nothing to compare" \
            "" waived
    elif (( ${#digest_mismatch[@]} )); then
        record latest-digest-matches-version fail \
            ":latest and :${VERSION} disagree for: ${digest_mismatch[*]}"
    elif (( ${#unpublished[@]} == repos_seen )); then
        record latest-digest-matches-version not-measured "no promotion reached the digest check"
    else
        record latest-digest-matches-version pass
    fi
fi

# Both halves of the contract. Reachable on every path that gets here — the code above records
# an outcome for all six criteria regardless of `fail`, so this cannot turn a gate failure (1)
# into a wiring-misuse exit (2).
criteria_assert_all_checked

if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"promote","version":"%s","status":"%s","criteria":[%s],"next":%s}\n' \
        "$VERSION" "$([[ $fail -eq 0 ]] && echo pass || echo fail)" "$(criteria_json)" \
        "$([[ $fail -eq 0 ]] && echo '["finish"]' || echo '["investigate before publishing the GitHub release"]')"
fi
exit $fail
