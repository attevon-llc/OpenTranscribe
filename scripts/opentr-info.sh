#!/usr/bin/env bash
# Operator-facing text printers for opentr.sh (not shipped to installs: opentranscribe.sh never
# calls these). Sourced by opentr.sh; moved out of common.sh and opentr.sh to keep both under
# their size baselines.

# Display quick reference commands
print_help_commands() {
  echo "⚡ Quick Commands Reference:"
  echo "   - Reset environment: ./opentr.sh reset [dev|prod]"
  echo "   - Stop all services: ./opentr.sh stop"
  echo "   - View logs: ./opentr.sh logs [service_name]"
  echo "   - Restart backend: ./opentr.sh restart-backend"
  echo "   - Backend code: hot-reloads (API); celery workers: ./opentr.sh restart-backend"
  echo "   - Frontend code: hot-reloads (Vite)"
  echo "   - Rebuild only after requirements*.txt, package-lock.json or Dockerfile changes:"
  echo "     ./opentr.sh rebuild-backend / ./opentr.sh rebuild-frontend"
}

# Print access information for all services
# Detects NGINX configuration and shows appropriate URLs
print_access_info() {
  # Check if NGINX is configured (via NGINX_SERVER_NAME env var or .env file)
  # In dev mode, NGINX is never used (Vite serves frontend directly)
  local domain=""
  local protocol="https"
  local https_port="${NGINX_HTTPS_PORT:-443}"

  # Only show NGINX info in production mode
  if [ "${ENVIRONMENT:-dev}" != "dev" ]; then
    # Check environment variable first
    if [ -n "${NGINX_SERVER_NAME:-}" ]; then
      domain="$NGINX_SERVER_NAME"
    # Then check .env file
    elif [ -f .env ]; then
      domain=$(read_env_value NGINX_SERVER_NAME .env)
      https_port=$(read_env_value NGINX_HTTPS_PORT .env)
      https_port="${https_port:-443}"
    fi
  fi

  echo ""
  if [ -n "$domain" ]; then
    # NGINX reverse proxy mode - single entry point with HTTPS
    local port_suffix=""
    if [ "$https_port" != "443" ]; then
      port_suffix=":$https_port"
    fi

    echo "🔒 NGINX Reverse Proxy Mode (HTTPS)"
    echo "🌐 Access the application at:"
    echo "   - Frontend:          ${protocol}://${domain}${port_suffix}"
    echo "   - API:               ${protocol}://${domain}${port_suffix}/api"
    echo "   - API Documentation: ${protocol}://${domain}${port_suffix}/api/docs"
    echo "   - Flower Dashboard:  ${protocol}://${domain}${port_suffix}/flower/"
    echo "   - MinIO Console:     ${protocol}://${domain}${port_suffix}/minio/"
    echo "   - Documentation:     ${protocol}://${domain}${port_suffix}/docs/"
    echo ""
    echo "📝 Note: Browser microphone recording is now available via HTTPS!"
    echo "   If you see certificate warnings, trust the certificate on your device."
    echo "   See: https://docs.opentranscribe.app/docs/installation/small-business-lan"
  else
    # Direct container access mode (development default).
    # Read the same *_PORT variables compose interpolates into its `ports:`
    # entries, so a custom .env layout — or a fresh stack's --port-offset,
    # which exports them offset — prints the ports actually published.
    echo "🌐 Access the application at:"
    echo "   - Frontend:            http://localhost:${FRONTEND_PORT:-5173}"
    echo "   - API:                 http://localhost:${BACKEND_PORT:-5174}/api"
    echo "   - API Documentation:   http://localhost:${BACKEND_PORT:-5174}/docs"
    echo "   - MinIO Console:       http://localhost:${MINIO_CONSOLE_PORT:-5179}"
    echo "   - Flower Dashboard:    http://localhost:${FLOWER_PORT:-5175}/flower"
    echo "   - OpenSearch:          http://localhost:${OPENSEARCH_PORT:-5180}"
    echo "   - Documentation:       http://localhost:${DOCS_PORT:-5183}/docs/"
    echo ""
    echo "📝 Note: Microphone recording only works on localhost in this mode."
    echo "   For HTTPS access from other devices, set NGINX_SERVER_NAME in .env"
    echo "   See: https://docs.opentranscribe.app/docs/installation/small-business-lan"
  fi
  echo ""
}

# Verify a baked (--no-bindmount) stack is actually RUNNING the code it claims to
# (issue #528). Compose has been observed accepting `--force-recreate` on the wire
# and still leaving old containers up, so after `up` we read GIT_SHA back from
# every baked service and compare it to the SHA this run exported. A mismatch is
# self-healed once with the surgical per-service recreate (measured to work where
# the blanket flag did not), then re-checked; if it STILL disagrees we exit 1 —
# a baked stack on stale code silently invalidates every measurement taken
# against it, which is worse than a failed start.
#
# $1 = the compose "-f ..." chain for this deployment. Uses $GIT_SHA (exported
# at script top) as the expected value. Containers reporting nothing or
# "unknown" count as stale: an unstamped container cannot attribute a
# measurement either.
fresh_verify_baked_git_sha() {
  local files="$1"
  local expected="$GIT_SHA"
  local attempt svc cid got stale
  for attempt in 1 2; do
    stale=()
    for svc in "${FRESH_BAKED_SERVICES[@]}"; do
      # shellcheck disable=SC2086
      cid="$(docker compose $files ps -q "$svc" 2>/dev/null)"
      [ -z "$cid" ] && continue # not part of this deployment (profile off)
      got="$(docker exec "$cid" printenv GIT_SHA 2>/dev/null || echo '<unset>')"
      if [ "$got" != "$expected" ]; then
        stale+=("$svc")
        echo "   ✗ ${svc}: GIT_SHA=${got} (expected ${expected})"
      fi
    done
    if [ "${#stale[@]}" -eq 0 ]; then
      echo "🔏 Baked-stack code verified: GIT_SHA=${expected} on every baked service."
      return 0
    fi
    if [ "$attempt" -eq 1 ]; then
      echo "⚠️  ${#stale[@]} baked service(s) are running STALE code — recreating surgically (issue #528)..."
      # shellcheck disable=SC2086
      docker compose $files up -d --no-deps --force-recreate "${stale[@]}"
    fi
  done
  echo ""
  echo "❌ Baked stack is STILL running stale code after a surgical recreate (issue #528)."
  echo "   Any measurement against this stack would describe code nobody is running."
  echo "   Inspect: docker compose ${files} ps   and compare printenv GIT_SHA per container."
  exit 1
}
