# shellcheck shell=bash
# The lite and public-demo overlays for ./opentr.sh start/reset (issue #628).
#
# Sourced by opentr.sh; takes the caller's lite and demo flags as arguments ("" when unset)
# and appends to COMPOSE_FILES, after the nginx overlay (as opentranscribe.sh orders them). The demo
# implies lite: it runs the CPU-only image and keeps DEPLOYMENT_MODE=lite, while DEMO_MODE
# (set by docker-compose.demo.yml) is what refuses writes, WebSockets and task publishes.
# The installed deployment selects the same overlay from DEMO_MODE=true in .env
# (opentranscribe.sh get_compose_files).

add_lite_and_demo_overlays() {
  local lite_flag="${1:-}" demo_flag="${2:-}"
  if [ -n "$lite_flag" ] || [ -n "$demo_flag" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.lite.yml"
    echo "☁️  Adding lite overlay (docker-compose.lite.yml)"
  fi
  if [ -n "$demo_flag" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.demo.yml"
    echo "🔒 Adding public read-only demo overlay (docker-compose.demo.yml)"
    # Behind nginx the public demo sits behind Cloudflare: the same rule as the installed chain
    # (opentranscribe.sh compose_overlay_demo), so a --demo stack runs what the demo host runs.
    # Needs the nginx overlay already in COMPOSE_FILES (opentr.sh calls add_nginx_overlay first).
    if [[ " $COMPOSE_FILES " == *" -f docker-compose.nginx.yml "* && -f docker-compose.demo-cloudflare.yml && -f nginx/cloudflare-real-ip.conf ]]; then
      COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.demo-cloudflare.yml"
      echo "☁️  Adding Cloudflare real-IP overlay for the demo's nginx (docker-compose.demo-cloudflare.yml)"
    fi
  fi
}
