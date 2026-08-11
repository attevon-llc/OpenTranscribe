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
log() { echo -e "${BLUE}[bump]${NC} $*" >&2; }
ok()  { echo -e "${GREEN}[bump] ✓${NC} $*" >&2; }
die() { echo -e "${RED}[bump] ✗${NC} $*" >&2; exit "${2:-1}"; }

VERSION="${1:-${RELEASE_VERSION:-}}"
[[ -n "$VERSION" ]] || die "usage: 20-bump.sh vX.Y.Z" 2
VERSION="$(ver_normalize "$VERSION")"
ver_is_valid "$VERSION" || die "not a semver: $VERSION" 2
SEMVER="$(ver_semver "$VERSION")"
NO_COMMIT="${NO_COMMIT:-false}"

CURRENT="$(ver_to_version)"
if ! ver_lt "$CURRENT" "$VERSION"; then
    die "refusing to bump $CURRENT -> $VERSION (not an increase; the migration chain is one-way)"
fi
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

# 4. frontend/package-lock.json — regenerated, not edited (two version fields)
if command -v npm >/dev/null 2>&1; then
    (cd frontend && npm install --package-lock-only --silent)
    ok "frontend/package-lock.json (npm install --package-lock-only)"
else
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
    ok "CHANGELOG.md ([Unreleased] -> [$SEMVER], fresh [Unreleased] opened)"
else
    log "no [Unreleased] section — leaving CHANGELOG.md alone"
fi

# There is deliberately NO expected-schemas.tsv row to append. That table was
# deleted: the Alembic head is derived from the down_revision graph
# (scripts/release-tests/lib/alembic-head.py), so there is nothing to forget.

log "verifying the bump before committing"
if ! python3 scripts/release/check-version-consistency.py --mode ci; then
    die "version sources still disagree after the bump — NOT committing"
fi

if [[ "$NO_COMMIT" == "true" ]]; then
    ok "bump written (NO_COMMIT=true, nothing committed)"
    exit 0
fi

git add VERSION pyproject.toml frontend/package.json frontend/package-lock.json CHANGELOG.md
git commit -m "chore(release): bump version to ${SEMVER}

Written by scripts/release/20-bump.sh across every version source:
VERSION, pyproject.toml, frontend/package.json, frontend/package-lock.json
(regenerated — it carries the version twice), and CHANGELOG.md.

Verified with check-version-consistency.py before committing."

ok "committed"
