#!/usr/bin/env bash
# demo-upgrade.sh — move the public demo to the newest published release (issue #628, E3).
#
# Pull-based (decision D10): run on the demo host from a systemd timer
# (scripts/demo/systemd/opentranscribe-demo-upgrade.timer). The host stores no deploy
# credential; it reads the public GitHub "latest release" record and pulls public images.
#
# Sequence (docs-site/docs/operations/public-demo.md, "Upgrades"):
#   1. lock (one upgrade or reset at a time);
#   2. target = DEMO_TARGET_TAG, else the latest published release's tag_name;
#   3. same as OT_IMAGE_TAG -> nothing to do (exit 0);
#   4. ./opentranscribe.sh update --version <target>  (pins OT_IMAGE_TAG, records the
#      previous tag, pulls, restarts with its own /health wait), then update-full (the
#      compose files and scripts of that tag);
#   5. demo-reset.sh (fresh seed against the new schema; the demo resets on every upgrade);
#   6. /health must answer 200 with the target version;
#   7. on any failure: update --rollback, update-full, reset, health, and exit 1.
# Every upgrade ends in a reset from the seed, so the one-way migration chain never blocks
# the rollback here.
#
# Usage: scripts/demo/demo-upgrade.sh [--dry-run]
# Environment (all optional):
#   DEMO_INSTALL_DIR     install directory holding .env and opentranscribe.sh (default: cwd)
#   DEMO_TARGET_TAG      pin a target, e.g. a pre-release (decision D9)
#   DEMO_RELEASE_API     default https://api.github.com/repos/attevon-llc/OpenTranscribe/releases/latest
#   DEMO_HEALTH_URL      default http://127.0.0.1:${BACKEND_PORT:-5174}/health
#   DEMO_HEALTH_TIMEOUT  seconds, default 900 (DEMO_HEALTH_INTERVAL between polls, default 10)
#   DEMO_RESET_CMD       default: demo-reset.sh beside this script
#   DEMO_STATUS_FILE     default: $DEMO_INSTALL_DIR/demo-status.json (read by the release
#                        `finish` stage)
#   DEMO_FRESH_NAME      act on the `--fresh NAME` stack instead of the demo (demo-target.sh)
# The target compose project is asserted before the first restart or volume delete.
# Exit: 0 upgraded or already current, 1 upgrade failed (rolled back), 2 misuse,
#       3 another upgrade/reset holds the lock, 4 rollback ALSO failed (page someone),
#       20-28 the target stack failed scripts/lib/stack_target.py (21 = not this install).

set -euo pipefail

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    "") ;;
    -h | --help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${DEMO_INSTALL_DIR:-$PWD}"
RELEASE_API="${DEMO_RELEASE_API:-https://api.github.com/repos/attevon-llc/OpenTranscribe/releases/latest}"
HEALTH_TIMEOUT="${DEMO_HEALTH_TIMEOUT:-900}"
HEALTH_INTERVAL="${DEMO_HEALTH_INTERVAL:-10}"
RESET_CMD="${DEMO_RESET_CMD:-$SCRIPT_DIR/demo-reset.sh}"
STATUS_FILE="${DEMO_STATUS_FILE:-$INSTALL_DIR/demo-status.json}"
TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'

cd "$INSTALL_DIR"
if [ ! -f .env ] || [ ! -x ./opentranscribe.sh ]; then
    echo "not an OpenTranscribe install directory: $INSTALL_DIR" >&2
    exit 2
fi

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d ' "'"'"; }

BACKEND_PORT="$(env_value BACKEND_PORT || true)"
HEALTH_URL="${DEMO_HEALTH_URL:-http://127.0.0.1:${BACKEND_PORT:-5174}/health}"

run() {
    echo "+ $*"
    [ "$DRY_RUN" = 1 ] || "$@"
}

exec 9>"$INSTALL_DIR/.demo.lock"
flock -n 9 || { echo "another demo upgrade or reset is running" >&2; exit 3; }

# shellcheck source=scripts/demo/demo-target.sh
source "$SCRIPT_DIR/demo-target.sh"
demo_assert_target || exit "$?"

resolve_target() {
    if [ -n "${DEMO_TARGET_TAG:-}" ]; then
        printf '%s\n' "$DEMO_TARGET_TAG"
        return
    fi
    curl -fsSL --max-time 30 -H 'Accept: application/vnd.github+json' "$RELEASE_API" |
        python3 -I -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])'
}

health_reports() {
    # 0 when /health answers 200 with version == $1 within the timeout.
    local want="$1" waited=0 body
    while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
        if body="$(curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null)" &&
            [ "$(printf '%s' "$body" | python3 -I -c 'import json, sys; print(json.load(sys.stdin).get("version", ""))')" = "$want" ]; then
            return 0
        fi
        sleep "$HEALTH_INTERVAL"
        waited=$((waited + HEALTH_INTERVAL))
    done
    return 1
}

write_status() {
    [ "$DRY_RUN" = 1 ] && return 0
    python3 -I - "$STATUS_FILE" "$1" "$2" "$3" <<'PY'
import datetime, json, sys
path, tag, previous, result = sys.argv[1:5]
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"tag": tag, "previous_tag": previous, "result": result,
               "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, fh)
PY
}

move_to() {
    # Pin, fetch that tag's config, reseed, and prove it with /health.
    run ./opentranscribe.sh update --version "$1" &&
        run ./opentranscribe.sh update-full &&
        run env DEMO_LOCK_HELD=1 "$RESET_CMD" &&
        { [ "$DRY_RUN" = 1 ] || health_reports "$1"; }
}

TARGET="$(resolve_target)"
if ! [[ "$TARGET" =~ $TAG_RE ]]; then
    echo "refusing target tag '$TARGET' (expected vX.Y.Z)" >&2
    exit 2
fi
CURRENT="$(env_value OT_IMAGE_TAG || true)"
CURRENT="${CURRENT:-latest}"

if [ "$TARGET" = "$CURRENT" ]; then
    echo "demo is already on $CURRENT"
    exit 0
fi

echo "upgrading the demo: $CURRENT -> $TARGET"
if move_to "$TARGET"; then
    write_status "$TARGET" "$CURRENT" upgraded
    echo "demo upgraded to $TARGET"
    exit 0
fi

echo "upgrade to $TARGET failed; rolling back to $CURRENT" >&2
if run ./opentranscribe.sh update --rollback &&
    run ./opentranscribe.sh update-full &&
    run env DEMO_LOCK_HELD=1 "$RESET_CMD" &&
    { [ "$DRY_RUN" = 1 ] || health_reports "$CURRENT"; }; then
    write_status "$CURRENT" "$TARGET" rolled-back
    exit 1
fi
write_status "$CURRENT" "$TARGET" rollback-failed
echo "ROLLBACK FAILED: the demo is down or half-upgraded; nginx stays stopped" >&2
exit 4
