#!/usr/bin/env bash
# Repair ownership of the pipeline's shared volumes.
#
# The backend image reserves /scratch/opentranscribe (with engine/ and diar/ subdirs) so a
# freshly created named volume inherits appuser (uid 1000). Volumes created by an older
# image predate that and are root-owned 0755, which the non-root workers cannot write: the
# engine's WAV handoff between the GPU and CPU stages then silently degrades to a
# re-decode, and the diar-native sidecar cannot be handed audio at all.
#
# Idempotent and safe to run on a live stack — it only touches ownership.
#
#   ./scripts/fix-shared-volume-perms.sh              # default project name
#   COMPOSE_PROJECT_NAME=myproj ./scripts/fix-shared-volume-perms.sh
set -euo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")}"
# appuser is `useradd -u 1000` (explicit) but `groupadd -r appuser` (a system group with
# no explicit GID pin) — it lands at 999, not 1000, verified live via `id appuser` in the
# built image. A volume the Dockerfile chowns at build time is 1000:999; this script's
# default used to be 1000:1000, which doesn't exist in the image, so a volume repaired by
# this script diverged from one created fresh by the image itself (issue #580).
UID_GID="${SHARED_VOLUME_OWNER:-1000:999}"

# Issue #661 E2: the pipeline consolidated onto ONE volume, pipeline_scratch, with three
# namespaces (<file_uuid>/, engine/, diar/) — transcription-temp and diar-native-tmp no
# longer exist on a fresh install. Only pipeline_scratch may satisfy the "fixed -eq 0"
# refusal below: a stack that still HAS the two legacy volumes (not yet upgraded/pruned)
# but a root-owned pipeline_scratch must not report success just because the legacy repair
# happened to touch something. Legacy volumes are still repaired when present (harmless,
# keeps a rollback to an older image usable) but never counted.
VOLUMES=(pipeline_scratch)
LEGACY_VOLUMES=(transcription-temp diar-native-tmp)

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
  # chmod the two reserved namespace subdirs too (issue #661 E2) so a fresh
  # `os.makedirs(exist_ok=True)` under a repaired-but-not-yet-created parent inherits
  # correctly; `mkdir -p` here is a no-op if the runtime already created them.
  docker run --rm -v "$full":/v alpine:3 sh -c \
    'mkdir -p /v/engine /v/diar && chown "'"$UID_GID"'" /v/engine /v/diar && chmod 775 /v/engine /v/diar'
  after=$(docker run --rm -v "$full":/v alpine:3 stat -c '%u:%g %a' /v)
  echo "  $full: $before -> $after"
  fixed=$((fixed + 1))
done

for vol in "${LEGACY_VOLUMES[@]}"; do
  full="${PROJECT}_${vol}"
  if ! docker volume inspect "$full" >/dev/null 2>&1; then
    continue
  fi
  before=$(docker run --rm -v "$full":/v alpine:3 stat -c '%u:%g %a' /v)
  docker run --rm -v "$full":/v alpine:3 chown -R "$UID_GID" /v
  after=$(docker run --rm -v "$full":/v alpine:3 stat -c '%u:%g %a' /v)
  echo "  $full: $before -> $after (legacy volume, repaired but not counted)"
done

# A run that repairs zero volumes because every one of them is genuinely absent (a
# never-started stack) looks identical, from this script's own output, to one where
# $PROJECT resolved to the wrong compose project (this checkout's directory name is NOT
# necessarily the compose project the volumes were created under — a git worktree is the
# common case) and every `docker volume inspect` missed for that reason instead. Fail
# loudly rather than silently reporting success either way (issue #602) — a caller that
# genuinely expects "nothing to repair yet" can check for this exact message.
if [ "$fixed" -eq 0 ]; then
  echo "❌ repaired 0 volume(s) for project '$PROJECT' -- every volume was absent." >&2
  echo "   If this project has never been started, that's expected -- ignore this." >&2
  echo "   Otherwise \$PROJECT likely resolved wrong. Pass the real one explicitly:" >&2
  echo "     COMPOSE_PROJECT_NAME=<actual-project> $0" >&2
  exit 1
fi

echo "repaired $fixed volume(s); restart the workers to pick it up:"
echo "  docker compose restart celery-worker celery-cpu-worker"
