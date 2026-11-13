#!/usr/bin/env bash
# Sourced by opentr.sh: build_prod_images (backend + frontend + docs, concurrently).
# Needs APP_VERSION and GIT_SHA exported by the caller.

# Build production images locally (backend + frontend)
build_prod_images() {
  local tag="${OT_IMAGE_TAG:-latest}" # a --fresh prod deployment tags its own (fresh_prod_dispatch)
  echo "🥽 Building production Docker images locally (tag: ${tag})..."

  # Build identity (issue #1273). The backend build context is ./backend, so the repo-root
  # VERSION file is not in the image: without these args a locally built prod backend reports
  # "unknown", which silences the About modal's stale-tab warning and its exact source link.
  # APP_VERSION and GIT_SHA are exported at the top of this script; the frontend takes the
  # same GIT_SHA so both sides of one build name the same commit.
  local build_time
  build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # The three builds share nothing, so run them concurrently (owner 2026-11-12, WASTE_AUDIT W14).
  # Each logs to its own file (replayed in order afterwards so output is not interleaved) and is
  # reaped with its own `wait <pid>` so a failure in any one cannot be masked by the others.
  local logdir pids=()
  logdir="$(mktemp -d -t ot-prodbuild.XXXXXX)"
  echo "🧱 Building backend, frontend and docs images concurrently (tag: ${tag})..."

  docker build \
    --build-arg "APP_VERSION=${APP_VERSION}" \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    --build-arg "BUILD_TIME=${build_time}" \
    -t "davidamacey/opentranscribe-backend:${tag}" -f backend/Dockerfile.prod backend \
    >"$logdir/backend.log" 2>&1 &
  pids+=("$!")

  docker build \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    -t "davidamacey/opentranscribe-frontend:${tag}" -f frontend/Dockerfile.prod frontend \
    >"$logdir/frontend.log" 2>&1 &
  pids+=("$!")

  # The docs build context is docs-site/, so pass the repo-root VERSION in explicitly --
  # docusaurus.config.ts cannot read ../VERSION from inside the container.
  docker build --build-arg DOCS_BASE_URL=/docs/ \
    --build-arg "OT_VERSION=${APP_VERSION}" \
    -t "davidamacey/opentranscribe-docs:${tag}" docs-site \
    >"$logdir/docs.log" 2>&1 &
  pids+=("$!")

  local failed=0 name i=0
  for name in backend frontend docs; do
    if wait "${pids[$i]}"; then
      echo "✅ ${name} image built"
    else
      echo "❌ ${name} image build failed"
      failed=1
    fi
    i=$((i + 1))
    echo "── ${name} build log ──"
    cat "$logdir/${name}.log"
  done
  rm -rf "$logdir"
  [ "$failed" -eq 0 ] || exit 1

  echo "✅ Local production images built successfully"
}
