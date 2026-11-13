#!/bin/sh
# Writes the Basic credential nginx adds to /flower/ requests once auth_request has admitted
# an admin. Flower runs with --basic_auth (docker-compose.yml), so a request without it gets
# Flower's own 401 even after the backend said yes.
#
# The official nginx image runs every executable *.sh in /docker-entrypoint.d/ before it
# starts nginx (docker-library/nginx docker-entrypoint.sh). Its envsubst template step cannot
# do this job alone: RFC 7617 needs base64(user-id ":" password), which envsubst cannot
# compute. Writing an include file also keeps nginx.conf and nginx-pki.conf plain configs, so
# every place that mounts them as conf.d/default.conf keeps working unchanged.
#
# The secret comes from the container environment at start; nothing is baked into the image.
# With no FLOWER_PASSWORD the header is cleared, so Flower keeps refusing (fail closed).
# FLOWER_AUTH_CONF moves the output, for a host-run nginx (scripts/pki/nginx-pki-dev.conf).
set -eu

out="${FLOWER_AUTH_CONF:-/etc/nginx/flower-auth/authorization.conf}"
umask 077

if [ -z "${FLOWER_PASSWORD:-}" ]; then
    echo "$0: FLOWER_PASSWORD is not set; /flower/ will answer Flower's own 401" >&2
    # Still clear the header, so a client cannot supply a credential of its own.
    printf 'proxy_set_header Authorization "";\n' >"$out"
    exit 0
fi

credential="$(printf '%s:%s' "${FLOWER_USER:-admin}" "$FLOWER_PASSWORD" | base64 | tr -d '\n')"
printf 'proxy_set_header Authorization "Basic %s";\n' "$credential" >"$out"
