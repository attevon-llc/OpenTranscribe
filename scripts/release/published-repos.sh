#!/bin/bash
# The set of Docker Hub repos a release MUST have published — derived, never transcribed.
#
# Sourced by 90-promote.sh and 95-finish.sh. Both used to carry their own literal list and
# the two disagreed with each other AND with reality:
#
#   90-promote.sh:26   for repo in backend backend-lite frontend docs   # + missing = SKIP
#   95-finish.sh:28    for repo in backend frontend                     # lite AND docs absent
#
# So `finish` — the stage that points every new user at the version, and the gist for #667
# called "the strongest backstop; add lite here even if you add it nowhere else" — would
# publish a GitHub Release marked --latest while opentranscribe-backend-lite:$VERSION did
# not exist at all. On arm64 that is not a partial release: opentranscribe.sh defaults arm64
# hosts to DEPLOYMENT_MODE=lite (#680), so the lite image is the ONLY backend an arm64 user
# can pull, and its absence is a total install failure for that platform.
#
# `security-scan.sh list-repos` is the single home for the component→repo map (see
# scripts/CLAUDE.md, "The scannable component list has exactly one home"). 50-scan.sh and
# 80-publish.sh already derive from it; this makes promote and finish do the same, so adding
# a component is one edit rather than five.

# Emit `component<TAB>repo` for every component a normal release publishes.
#
# `blackwell` is excluded by name, exactly as 50-scan.sh:48 excludes it: it is built only on
# explicit request, never by `all`/`auto`, and it publishes a `:blackwell` tag rather than a
# `:vX.Y.Z` one — so demanding a versioned tag for it would fail every release. It also maps
# to the same repo as `backend`, so it would otherwise be a duplicate row.
release_published_repos() {
    local repo_root="${REPO_ROOT:-.}"
    "${repo_root}/scripts/security-scan.sh" list-repos 2>/dev/null \
        | awk -F'\t' 'NF == 2 && $1 != "blackwell"'
}

# Same list, or a hard exit 3 if it could not be derived.
#
# An empty list is "could not check", not "nothing to check" — the distinction #681 exists to
# preserve. Silently iterating over zero repos would turn a broken/renamed security-scan.sh
# into a green gate over a release nobody verified, which is the precise shape of the bug this
# file replaces. 3 = precondition unmet, per the release pipeline's stable exit codes.
release_published_repos_or_die() {
    local rows
    rows="$(release_published_repos)"
    if [[ -z "$rows" ]]; then
        echo "could not derive the published-repo list from security-scan.sh list-repos" >&2
        echo "  this is 'could not check', not 'nothing to check' — refusing to pass" >&2
        exit 3
    fi
    printf '%s\n' "$rows"
}
