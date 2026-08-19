#!/usr/bin/env bash
# Repair ownership of the pipeline's shared volumes.
#
# The backend image reserves /scratch/opentranscribe and /tmp/transcription so a freshly
# created named volume inherits appuser (uid 1000). Volumes created by an older image
# predate that and are root-owned 0755, which the non-root workers cannot write: the
# engine's WAV handoff between the GPU and CPU stages then silently degrades to a
# re-decode, and the diar-native sidecar cannot be handed audio at all.
#
# Idempotent and safe to run on a live stack — it only touches ownership.
#
#   ./scripts/fix-shared-volume-perms.sh              # default project name
#   COMPOSE_PROJECT_NAME=myproj ./scripts/fix-shared-volume-perms.sh
set -euo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")}"
UID_GID="${SHARED_VOLUME_OWNER:-1000:1000}"
VOLUMES=(pipeline_scratch transcription-temp)

echo "project: $PROJECT   owner: $UID_GID"
fixed=0
for vol in "${VOLUMES[@]}"; do
  full="${PROJECT}_${vol}"
  if ! docker volume inspect "$full" >/dev/null 2>&1; then
    echo "  $full: absent (created on first use — nothing to repair)"
    continue
  fi
  before=$(docker run --rm -v "$full":/v alpine:3 stat -c '%u:%g %a' /v)
  docker run --rm -v "$full":/v alpine:3 chown -R "$UID_GID" /v
  after=$(docker run --rm -v "$full":/v alpine:3 stat -c '%u:%g %a' /v)
  echo "  $full: $before -> $after"
  fixed=$((fixed + 1))
done

echo "repaired $fixed volume(s); restart the workers to pick it up:"
echo "  docker compose restart celery-worker celery-cpu-worker"
