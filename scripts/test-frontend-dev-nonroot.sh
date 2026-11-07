#!/usr/bin/env bash
# Isolated proof for issue #1372: the dev frontend image runs non-root and upgrades an existing
# root-owned node_modules/.svelte-kit volume with no manual step. Builds only the dev frontend
# image; uses throwaway containers/volumes labelled com.opentranscribe.proof=frontend-nonroot.
# Usage: scripts/test-frontend-dev-nonroot.sh <old-Dockerfile.dev> (root image to seed volumes)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OLD_DF=${1:?path to the pre-fix (root) frontend/Dockerfile.dev}
L=com.opentranscribe.proof=frontend-nonroot
SRC=$(mktemp -d -t ot-fn-proof.XXXXXX)
cleanup() {
  docker ps -aq --filter "label=$L" | xargs -r docker rm -f >/dev/null
  docker volume ls -q --filter "label=$L" | xargs -r docker volume rm >/dev/null
  rm -rf "$SRC"
}
trap cleanup EXIT

nice -n 19 docker build -q -f "$OLD_DF" -t ot-fn-test-old "$ROOT/frontend" >/dev/null
nice -n 19 docker build -q -f "$ROOT/frontend/Dockerfile.dev" -t ot-fn-test-new "$ROOT/frontend" >/dev/null
for v in nm1 sk1 nm2 sk2; do docker volume create --label "$L" "ot-fn-$v" >/dev/null; done
tar -C "$ROOT/frontend" --exclude=node_modules --exclude=.svelte-kit -cf - . | tar -C "$SRC" -xf -

check() { # image volume-prefix
  local img=$1 nm=$2 sk=$3 name=$4
  docker run -d --name "$name" --label "$L" -p 127.0.0.1:0:5173 \
    -v "$SRC:/app" -v "$nm:/app/node_modules" -v "$sk:/app/.svelte-kit" "$img" >/dev/null
  local port code=000
  for _ in $(seq 60); do
    port=$(docker port "$name" 5173/tcp | head -1 | sed 's/.*://')
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" || true)
    [ "$code" = 200 ] && break; sleep 2
  done
  echo "[$name] vite HTTP $code"
  echo "[$name] node process uids: $(docker exec "$name" ps -o user,args | grep -E 'vite' | grep -v grep | awk '{print $1}' | sort -u | tr '\n' ' ')"
  echo "[$name] numeric uid of vite process: $(docker exec "$name" sh -c 'p=$(ps -o pid,args | grep "[v]ite" | head -1 | awk "{print \$1}"); stat -c %u /proc/$p')"
  echo "[$name] node_modules owner: $(docker exec "$name" stat -c %u /app/node_modules) .vite-temp/.vite owner: $(docker exec "$name" sh -c 'stat -c %u /app/node_modules/.vite* 2>/dev/null | sort -u | tr "\n" " "')"
  echo "[$name] files in bind mount not owned by $(id -u): $(find "$SRC" -mindepth 1 -not -path "$SRC/node_modules" -not -path "$SRC/.svelte-kit" ! -user "$(id -u)" | tr "\n" " ")"
  [ "$code" = 200 ]
}

echo "== 1. EXISTING root-owned volumes (seeded by the old root image) =="
docker run --rm --label "$L" -v ot-fn-nm1:/app/node_modules -v ot-fn-sk1:/app/.svelte-kit ot-fn-test-old true
echo "seeded owner (old image): $(docker run --rm -v ot-fn-nm1:/v alpine stat -c %u /v)"
check ot-fn-test-new ot-fn-nm1 ot-fn-sk1 ot-fn-existing
echo "== 2. BRAND-NEW volumes =="
check ot-fn-test-new ot-fn-nm2 ot-fn-sk2 ot-fn-fresh
