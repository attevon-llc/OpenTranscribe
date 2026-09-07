#!/bin/bash
# Bump — write the new version to every source that carries one.
#
# There are five, and the old process bumped them by hand from a checklist. The
# checklist was accurate; people are not. This does all of them together or none
# of them, then verifies its own work with the consistency checker before
# committing, so a partial bump cannot reach a commit.
#
# package-lock.json is regenerated via npm rather than edited, because it carries
# the version TWICE (.version and .packages[""].version) and hand-editing reliably
# updates only the first.
#
# Exit: 0 written · 1 verification failed · 2 misuse

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=../release-tests/lib/versions.sh
source "$REPO_ROOT/scripts/release-tests/lib/versions.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
JSON_OUT="${JSON_OUT:-false}"
log() { echo -e "${BLUE}[bump]${NC} $*" >&2; }
ok()  { echo -e "${GREEN}[bump] ✓${NC} $*" >&2; }

# Severities from release-criteria.yaml; outcomes from here. Bidirectional — see
# criteria-lib.sh. Exported because the consumer lives across a file boundary.
export STAGE_ID=bump
# shellcheck source=scripts/release/criteria-lib.sh
source "$SCRIPT_DIR/criteria-lib.sh"

# `die` gains a criteria[] emission but keeps its exit code exactly: ${2:-1}, as before.
# It does NOT call criteria_assert_all_checked — on an early exit the later criteria really
# are unchecked, and the library exits 2 for that, which would rewrite a gate failure (1) or
# a misuse (2 from the usage checks) into something else.
#
# This stage had NO --json emitter at all, while scripts/release.sh's header states every
# stage emits one with a stable shape. That gap is closed here rather than left as the one
# stage an agent driving --json gets silence from.
die() {
    echo -e "${RED}[bump] ✗${NC} $1" >&2
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"bump","version":"%s","status":"fail","criteria":[%s],"next":["fix the finding above, then re-run: ./scripts/release.sh bump %s"]}\n' \
            "${VERSION:-}" "$(criteria_json)" "${VERSION:-vX.Y.Z}"
    fi
    exit "${2:-1}"
}

VERSION="${1:-${RELEASE_VERSION:-}}"
[[ -n "$VERSION" ]] || die "usage: 20-bump.sh vX.Y.Z" 2
VERSION="$(ver_normalize "$VERSION")"
ver_is_valid "$VERSION" || die "not a semver: $VERSION" 2
SEMVER="$(ver_semver "$VERSION")"
NO_COMMIT="${NO_COMMIT:-false}"

CURRENT="$(ver_to_version)"
if ! ver_lt "$CURRENT" "$VERSION"; then
    record version-increases fail "$CURRENT -> $VERSION is not an increase" \
        "pick a version above $CURRENT — the migration chain is one-way"
    die "refusing to bump $CURRENT -> $VERSION (not an increase; the migration chain is one-way)"
fi
record version-increases pass "$CURRENT -> $VERSION"
log "bumping $CURRENT -> $VERSION"

# 1. VERSION (the root source — everything else mirrors it)
echo "$VERSION" > VERSION
ok "VERSION"

# 2. pyproject.toml — only the [project] version, never a dependency pin
python3 - "$SEMVER" <<'PY'
import re, sys, pathlib
semver = sys.argv[1]
p = pathlib.Path("pyproject.toml")
text = p.read_text(encoding="utf-8")
# Anchored inside [project] so a `version = ` under another table is untouched.
new, n = re.subn(
    r'(\[project\](?:[^\[]|\n)*?\nversion\s*=\s*)"[^"]+"',
    rf'\g<1>"{semver}"',
    text,
    count=1,
)
if n != 1:
    raise SystemExit("could not locate [project].version in pyproject.toml")
p.write_text(new, encoding="utf-8")
PY
ok "pyproject.toml"

# 3. frontend/package.json — the top-level version only
python3 - "$SEMVER" <<'PY'
import json, sys, pathlib
semver = sys.argv[1]
p = pathlib.Path("frontend/package.json")
data = json.loads(p.read_text(encoding="utf-8"))
data["version"] = semver
# Preserve npm's 2-space formatting + trailing newline so the diff is one line.
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
ok "frontend/package.json"

# All three text sources are written above under `set -e`, so reaching this line means all
# three succeeded — a failed write aborts the script rather than falling through.
record all-sources-written pass "VERSION, pyproject.toml, frontend/package.json"

# 4. frontend/package-lock.json — regenerated, not edited (two version fields)
if command -v npm >/dev/null 2>&1; then
    (cd frontend && npm install --package-lock-only --silent)
    record package-lock-regenerated pass "npm install --package-lock-only"
    ok "frontend/package-lock.json (npm install --package-lock-only)"
else
    record package-lock-regenerated fail "npm not on PATH" \
        "install npm — package-lock.json carries the version twice and hand-editing updates only one"
    die "npm not found — package-lock.json carries the version twice and must be regenerated"
fi

# 5. CHANGELOG.md — promote [Unreleased] and open a fresh one
if grep -q '^## \[Unreleased\]' CHANGELOG.md; then
    python3 - "$SEMVER" <<'PY'
import datetime, pathlib, sys
semver = sys.argv[1]
today = datetime.date.today().isoformat()
p = pathlib.Path("CHANGELOG.md")
text = p.read_text(encoding="utf-8")
text = text.replace(
    "## [Unreleased]",
    f"## [Unreleased]\n\n## [{semver}] - {today}",
    1,
)
p.write_text(text, encoding="utf-8")
PY
    record changelog-promoted pass "[Unreleased] -> [$SEMVER]"
    ok "CHANGELOG.md ([Unreleased] -> [$SEMVER], fresh [Unreleased] opened)"
else
    # A documented no-op, and `warn` in the criteria file for that reason — 70-tag.sh is the
    # stage that actually refuses without a section for this version, so recording it as a
    # blocking failure here would move a gate the pipeline deliberately places later.
    record changelog-promoted not-measured "no [Unreleased] section to promote" \
        "add a '## [Unreleased]' section, or write '## [$SEMVER]' by hand before tagging"
    log "no [Unreleased] section — leaving CHANGELOG.md alone"
fi

# There is deliberately NO expected-schemas.tsv row to append. That table was
# deleted: the Alembic head is derived from the down_revision graph
# (scripts/release-tests/lib/alembic-head.py), so there is nothing to forget.

# The published roadmap regenerates on every docs deploy (deploy-docs.yml), so the
# LIVE site is never stale. The committed snapshot exists for the other consumer: the
# self-hosted docs Docker image, which has no network and no `gh`, so it ships whatever
# is in the tree at image-build time. Refreshing it here — in the same commit as the
# version bump — is what stops a released image carrying a months-old roadmap.
#
# Best-effort by design (criterion severity: warn). It needs `gh` and a reachable API,
# and a GitHub blip must not block a release over a docs data file when the live site is
# correct regardless.
log "refreshing the roadmap snapshot the docs image ships"
if python3 scripts/generate-roadmap.py >/dev/null 2>&1; then
    record roadmap-regenerated pass "docs-site/src/data/roadmap.json refreshed from the tracker"
    ok "roadmap.json"
else
    record roadmap-regenerated fail "generate-roadmap.py failed — the docs image will ship the committed snapshot" \
        "gh auth status && python3 scripts/generate-roadmap.py"
    log "roadmap regeneration failed — shipping the committed snapshot (see the warning above)"
fi

log "verifying the bump before committing"
if ! python3 scripts/release/check-version-consistency.py --mode ci; then
    record post-bump-consistency fail "version sources still disagree after the bump" \
        "python3 scripts/release/check-version-consistency.py --mode ci"
    die "version sources still disagree after the bump — NOT committing"
fi
record post-bump-consistency pass

if [[ "$NO_COMMIT" == "true" ]]; then
    # An explicit operator opt-out, so `waived` — a warning rather than a blocking
    # not-measured. Without the fifth argument this documented mode would fail its own stage.
    record bump-committed not-measured "NO_COMMIT=true — the operator asked for files only" \
        "git add … && git commit   # or re-run without NO_COMMIT" waived
    criteria_assert_all_checked
    ok "bump written (NO_COMMIT=true, nothing committed)"
    if [[ "$JSON_OUT" == "true" ]]; then
        printf '{"stage":"bump","version":"%s","status":"pass","criteria":[%s],"next":["review the diff, then commit it yourself (NO_COMMIT=true)"]}\n' \
            "$VERSION" "$(criteria_json)"
    fi
    exit 0
fi

# roadmap.json is staged here deliberately. Regenerating it without committing it would
# let roadmap-regenerated report `pass` while the docs image still shipped the old
# snapshot — a criterion that measures a write nobody keeps.
git add VERSION pyproject.toml frontend/package.json frontend/package-lock.json CHANGELOG.md \
        docs-site/src/data/roadmap.json
git commit -m "chore(release): bump version to ${SEMVER}

Written by scripts/release/20-bump.sh across every version source:
VERSION, pyproject.toml, frontend/package.json, frontend/package-lock.json
(regenerated — it carries the version twice), and CHANGELOG.md.

docs-site/src/data/roadmap.json is refreshed from the issue tracker in the same
commit: the docs Docker image has no network, so it ships whatever snapshot is
in the tree. The live site regenerates on every deploy and is unaffected.

Verified with check-version-consistency.py before committing."
record bump-committed pass

# Both halves of the contract, on the path where every criterion was reached.
criteria_assert_all_checked

ok "committed"
if [[ "$JSON_OUT" == "true" ]]; then
    printf '{"stage":"bump","version":"%s","status":"pass","criteria":[%s],"next":["verify"]}\n' \
        "$VERSION" "$(criteria_json)"
fi
