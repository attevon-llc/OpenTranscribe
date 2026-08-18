#!/bin/bash
#
# Regenerate backend/requirements.lock.txt from the RUNNING backend image (#492).
#
# The lock is generated from the image rather than resolved fresh on the host, and
# that is the whole point: the image is what ships, so its resolved tree is the
# known-good set. Resolving on the host would produce whatever upstream published
# today — which is the drift this file exists to stop.
#
# Regenerating is a deliberate, reviewable commit. That is also what makes a
# dependency bump visible in a PR diff instead of invisible in a rebuild.
#
# Usage:
#   ./scripts/lock-backend-deps.sh              # regenerate from the running container
#   ./scripts/lock-backend-deps.sh --check      # verify the lock matches; change nothing
#
set -euo pipefail

CONTAINER="${OT_BACKEND_CONTAINER:-opentranscribe-backend}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$REPO_ROOT/backend/requirements.lock.txt"
PYANNOTE_REPO="https://github.com/davidamacey/pyannote-audio.git"
PYANNOTE_BRANCH="gpu-optimizations"

CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "The backend container ($CONTAINER) is not running."
    echo "Start it with: ./opentr.sh start dev"
    exit 3
fi

# Everything the image installed from a DIRECT URL rather than from an index,
# rendered as an installable `name @ url` pin. Two packages qualify and a flat
# `pip freeze` gets both wrong:
#
#   pyannote.audio  a git dep, frozen as `pyannote-audio==4.0.5.dev16+ga3f38afb8`,
#                   which installs from nowhere;
#   en_core_web_sm  a spaCy model wheel from a GitHub release, frozen as
#                   `en_core_web_sm==3.8.0`, which is NOT on PyPI at all — the
#                   resolver fails outright ("No matching distribution found").
#
# Read from pip's own record rather than re-resolved: pinning `git ls-remote` would
# pin whatever the branch points at NOW, and `spacy download` picks whatever is
# compatible with the installed spaCy at build time. Both are the drift being
# fixed, so neither may be reintroduced by the generator.
direct_urls=$(docker exec "$CONTAINER" python -c "
import importlib.metadata as md, json
out = []
for d in md.distributions():
    try:
        raw = d.read_text('direct_url.json')
    except Exception:
        continue
    if not raw:
        continue
    info = json.loads(raw)
    name = d.metadata['Name']
    url = info['url']
    if 'vcs_info' in info:
        out.append(f\"{name} @ git+{url}@{info['vcs_info']['commit_id']}\")
    else:
        out.append(f'{name} @ {url}')
print('\n'.join(sorted(out)))
" 2>/dev/null || true)

installed_sha=$(printf '%s\n' "$direct_urls" | sed -n 's/.*pyannote-audio @ git+.*@\([0-9a-f]\{40\}\).*/\1/p' | head -1)

if [[ -z "$installed_sha" ]]; then
    echo "Could not read the installed pyannote.audio commit from $CONTAINER."
    echo "Without it the lock would pin a branch, which is what #492 is about."
    exit 1
fi

branch_sha=$(git ls-remote "$PYANNOTE_REPO" "$PYANNOTE_BRANCH" 2>/dev/null | cut -f1 || true)
if [[ -n "$branch_sha" && "$branch_sha" != "$installed_sha" ]]; then
    echo "NOTE: $PYANNOTE_BRANCH has moved since this image was built."
    echo "  image:  $installed_sha"
    echo "  branch: $branch_sha"
    echo "Pinning the IMAGE's commit — rebuild first if you meant to adopt the branch."
fi

generated=$(mktemp)
trap 'rm -f "$generated"' EXIT

# Everything above the first spec is prose and is preserved verbatim: it carries
# the three traps a flat `pip freeze` gets wrong.
awk '/^--extra-index-url/{exit} {print}' "$LOCK" > "$generated"
echo "--extra-index-url https://download.pytorch.org/whl/cu128" >> "$generated"
echo "" >> "$generated"

docker exec "$CONTAINER" python -m pip list --format=freeze 2>/dev/null \
  | grep -E '==' \
  | grep -viE '^(pip|setuptools|wheel|whisperx|faster-whisper|gliner|pyannote[._-]audio|en[._-]core[._-]web[._-]sm)==' \
  | sort -f >> "$generated"

printf '%s\n' "$direct_urls" >> "$generated"

if $CHECK_ONLY; then
    if diff -q "$generated" "$LOCK" >/dev/null; then
        echo "✅ requirements.lock.txt matches the running image"
        exit 0
    fi
    echo "❌ requirements.lock.txt does NOT match the running image:"
    diff "$LOCK" "$generated" | head -40
    echo ""
    echo "Regenerate with: ./scripts/lock-backend-deps.sh"
    exit 1
fi

cp "$generated" "$LOCK"
echo "✅ Regenerated $LOCK"
echo "   pyannote.audio pinned to ${installed_sha}"
grep -cE '^[a-zA-Z]' "$LOCK" | xargs echo "   specs:"
