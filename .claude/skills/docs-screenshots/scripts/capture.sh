#!/bin/bash
# Capture one docs-site screenshot from the live dev stack.
#
# Usage: capture.sh <category> <name> [browse.js action ...]
#
# Logs in as admin@example.com, runs the given browse.js action sequence, takes a
# final screenshot, and copies it into docs-site/static/img/screenshots/<category>/<name>.png.
# Pass 'click:.theme-toggle' as the first action for a light-mode variant (dark is default).
set -e

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <category> <name> [browse.js action ...]" >&2
  exit 1
fi

CATEGORY="$1"
NAME="$2"
shift 2
EXTRA_ACTIONS=("$@")

if ! command -v node >/dev/null 2>&1; then
  echo "node not found" >&2
  exit 1
fi

BROWSE_JS="$HOME/bin/browser-tools/browse.js"
if [[ ! -f "$BROWSE_JS" ]]; then
  echo "browse.js not found at $BROWSE_JS — see ~/bin/browser-tools/README.md" >&2
  exit 1
fi

APP_URL="${APP_URL:-http://localhost:5173}"
SCREENSHOTS_DIR="$HOME/bin/browser-tools/screenshots"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
TARGET_DIR="$REPO_ROOT/docs-site/static/img/screenshots/$CATEGORY"

mkdir -p "$TARGET_DIR"

DISPLAY_ARGS=()
if [[ -n "${SCREENSHOT_DISPLAY:-}" ]]; then
  DISPLAY_ARGS=("--display=${SCREENSHOT_DISPLAY}")
fi

echo "=== Capturing $CATEGORY/$NAME ==="
node "$BROWSE_JS" "$APP_URL" "${DISPLAY_ARGS[@]}" --timeout=60000 \
  'fill:#email:admin@example.com' \
  'fill:#password:password' \
  'click:button[type=submit]' \
  'wait:3000' \
  "${EXTRA_ACTIONS[@]}" \
  "screenshot:$NAME"

cp "$SCREENSHOTS_DIR/$NAME.png" "$TARGET_DIR/$NAME.png"
echo "-> $TARGET_DIR/$NAME.png"
