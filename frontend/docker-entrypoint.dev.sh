#!/bin/sh
# Dev frontend entrypoint (issue #1372): repair volume ownership, then drop root.
#
# Anonymous volumes at /app/node_modules and /app/.svelte-kit are reused across `up` runs and
# Docker does not re-own an existing volume (only an empty one is populated from the image), so
# stacks created by the old root image keep root-owned volumes. Without this, Vite dies with
# EACCES on /app/node_modules/.vite-temp. The bind-mounted source is never touched.
set -eu

# Run as the owner of the bind mount so files Vite writes there belong to the host developer.
uid=$(stat -c %u /app)
gid=$(stat -c %g /app)
if [ "$uid" = 0 ]; then
  uid=$(id -u node)
  gid=$(id -g node)
fi

if [ "$(id -u)" = 0 ]; then
  for d in /app/node_modules /app/.svelte-kit; do
    # Recurse only when the top-level owner is wrong, so later starts are a single stat.
    if [ -d "$d" ] && [ "$(stat -c %u "$d")" != "$uid" ]; then
      chown -R "$uid:$gid" "$d"
    fi
  done
  if [ "$uid" != "$(id -u node)" ]; then
    export HOME=/tmp
  fi
  exec su-exec "$uid:$gid" "$@"
fi
exec "$@"
