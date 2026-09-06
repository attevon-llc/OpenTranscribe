#!/usr/bin/env bash
# Regenerate the interactive architecture diagrams shown at /architecture on the
# docs site from their checked-in specs.
#
# The specs (docs-site/architecture-diagrams/specs/*.json) are the source of truth —
# hand-authored from real repo evidence (routers, Celery task_routes, services/
# layout, frontend directory structure), not something a static-analysis tool
# produced. The rendered HTML (docs-site/static/architecture/*.html) is a build
# artifact: regenerate it here rather than hand-editing it.
#
# Requires the Archify Claude Code skill (MIT, https://github.com/tt-a1i/archify):
#   npx skills add tt-a1i/archify -g
#
# Usage:
#   scripts/generate-architecture-diagrams.sh            # regenerate all
#   scripts/generate-architecture-diagrams.sh search-indexing   # just one spec

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC_DIR="$REPO_ROOT/docs-site/architecture-diagrams/specs"
OUT_DIR="$REPO_ROOT/docs-site/static/architecture"

ARCHIFY_DIR="${ARCHIFY_SKILL_DIR:-}"
if [ -z "$ARCHIFY_DIR" ]; then
  for candidate in "$HOME/.agents/skills/archify" "$HOME/.claude/skills/archify"; do
    if [ -f "$candidate/bin/archify.mjs" ]; then
      ARCHIFY_DIR="$candidate"
      break
    fi
  done
fi
if [ -z "$ARCHIFY_DIR" ] || [ ! -f "$ARCHIFY_DIR/bin/archify.mjs" ]; then
  echo "error: Archify skill not found." >&2
  echo "  Install it with: npx skills add tt-a1i/archify -g" >&2
  echo "  Or point at an existing checkout: ARCHIFY_SKILL_DIR=/path/to/archify $0" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

shopt -s nullglob
specs=("$SPEC_DIR"/*.json)
shopt -u nullglob
if [ "${#specs[@]}" -eq 0 ]; then
  echo "error: no specs found in $SPEC_DIR" >&2
  exit 1
fi

filter="${1:-}"
status=0
for spec in "${specs[@]}"; do
  name="$(basename "$spec")"
  name="${name%.*.json}"
  if [ -n "$filter" ] && [ "$name" != "$filter" ]; then
    continue
  fi
  diagram_type="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['diagram_type'])" "$spec")"
  out="$OUT_DIR/$name.html"
  echo "==> $name ($diagram_type)"
  if ! node "$ARCHIFY_DIR/bin/archify.mjs" validate "$diagram_type" "$spec" --quality showcase --json > /tmp/archify-validate-"$name".json 2>&1; then
    echo "    VALIDATION FAILED — see /tmp/archify-validate-$name.json" >&2
    status=1
    continue
  fi
  node "$ARCHIFY_DIR/bin/archify.mjs" deliver "$diagram_type" "$spec" "$out" --quality showcase --json \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('    delivered:', d['validation']['compositionStatus'], d['artifact']['bytes'], 'bytes')"
done

exit $status
