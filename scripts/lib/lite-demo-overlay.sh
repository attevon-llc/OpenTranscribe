# shellcheck shell=bash
# The lite and public-demo overlays for ./opentr.sh start/reset (issue #628).
#
# Sourced by opentr.sh; reads LITE_FLAG and DEMO_FLAG and appends to COMPOSE_FILES. The demo
# implies lite: it runs the CPU-only image and keeps DEPLOYMENT_MODE=lite, while DEMO_MODE
# (set by docker-compose.demo.yml) is what refuses writes, WebSockets and task publishes.
# The installed deployment selects the same overlay from DEMO_MODE=true in .env
# (opentranscribe.sh get_compose_files).

add_lite_and_demo_overlays() {
  if [ -n "${LITE_FLAG:-}" ] || [ -n "${DEMO_FLAG:-}" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.lite.yml"
    echo "☁️  Adding lite overlay (docker-compose.lite.yml)"
  fi
  if [ -n "${DEMO_FLAG:-}" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.demo.yml"
    echo "🔒 Adding public read-only demo overlay (docker-compose.demo.yml)"
  fi
}
