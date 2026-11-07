#!/bin/bash
# `./opentr.sh start prod --fresh <name>`: the production half of --fresh isolation (#1264 item 5).
# Sourced by opentr.sh (it appends to start_app's locals _port_vars/_aux_files/_aux_services and
# reads ENVIRONMENT/BUILD_FLAG/WITH_PKI_FLAG), never run on its own.
#
# Dev --fresh needs none of this: its base chain is docker-compose.override.yml. A prod chain
# also loads docker-compose.prod.yml (+ local.yml for --build), and optionally the nginx proxy,
# which hard-codes a container_name and publishes 80/443 on the host.

# Host ports docker-compose.nginx.yml publishes; moved by the same *_PORT-variable route as every
# other port (an overlay `ports:` list would be APPENDED by compose, issue #343).
FRESH_NGINX_PORT_VARS=(
  "NGINX_HTTP_PORT=80"          # nginx redirect listener → :80
  "NGINX_HTTPS_PORT=443"        # nginx TLS listener      → :443
)
FRESH_NGINX_SERVICES=(nginx)
# docker-compose.pki.yml (the PROD PKI overlay) publishes only the mTLS listener; the plain
# PKI_HTTP_PORT belongs to docker-compose.pki-dev.yml.
FRESH_PKI_PROD_PORT_VARS=("PKI_HTTPS_PORT=5182")
FRESH_PKI_PROD_FILES=(docker-compose.pki.yml scripts/pki/test-certs/pki-test.compose.yml)

# 0 when add_nginx_overlay will load the proxy (the single predicate for both).
nginx_overlay_wanted() {
  [ -n "${NGINX_SERVER_NAME:-}" ] && [ "${ENVIRONMENT:-}" = "prod" ] && [ -f "docker-compose.nginx.yml" ]
}

# Record the prod chain and its published ports/names for a fresh deployment. No-op in dev.
fresh_prod_dispatch() {
  [ "${ENVIRONMENT:-}" = "prod" ] || return 0
  _aux_files+=("docker-compose.prod.yml")
  # --build runs under docker-compose.local.yml, and its images must not retag the main
  # stack's `:latest` (docker-compose.prod.yml interpolates OT_IMAGE_TAG).
  if [ "${BUILD_FLAG:-}" = "--build" ]; then
    _aux_files+=("docker-compose.local.yml")
    export OT_IMAGE_TAG="$FRESH_PROJECT"
  fi
  if nginx_overlay_wanted; then
    _port_vars+=("${FRESH_NGINX_PORT_VARS[@]}")
    _aux_services+=("${FRESH_NGINX_SERVICES[@]}")
    _aux_files+=("docker-compose.nginx.yml")
  fi
  if [ -n "${WITH_PKI_FLAG:-}" ]; then
    _port_vars+=("${FRESH_PKI_PROD_PORT_VARS[@]}")
    _aux_files+=("${FRESH_PKI_PROD_FILES[@]}")
  fi
}
