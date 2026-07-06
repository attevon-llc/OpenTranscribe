#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
ENV_EXT=".env"
DEFAULT_ENV_FILE="${ROOT_DIR}/native-loopback${ENV_EXT}.example"
ENV_FILE="${OPENTRANSCRIBE_NATIVE_ENV:-$DEFAULT_ENV_FILE}"
PYTHON_BIN="${OPENTRANSCRIBE_PYTHON:-}"

resolve_python() {
  if [[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]]; then
    return
  fi
  local venv_dir
  venv_dir="$(find "$ROOT_DIR/.." -maxdepth 3 -name pyvenv.cfg -exec dirname {} \; | head -1)"
  PYTHON_BIN="${venv_dir:-}/bin/python3"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3)"
  fi
}

ensure_opensearch() {
  local os_host="${OPENSEARCH_HOST:-127.0.0.1}" os_port="${OPENSEARCH_PORT:-9200}"
  curl -s -X PUT "http://${os_host}:${os_port}/_cluster/settings" \
    -H 'Content-Type: application/json' \
    -d '{"transient":{"cluster.blocks.create_index":false},"persistent":{"cluster.blocks.create_index":null}}' \
    >/dev/null 2>&1 || true
}

load_env() {
  resolve_python
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
  export STORAGE_BACKEND="${STORAGE_BACKEND:-filesystem}"
  export STORAGE_LOCAL_ROOT="${STORAGE_LOCAL_ROOT:-$HOME/.opentranscribe/storage}"
  # Embedded SQLite serves search natively (no OpenSearch/JVM daemon). Set
  # SEARCH_BACKEND=opensearch to roll back to the Lucene path; the code default
  # stays opensearch so only this native launch cuts over.
  export SEARCH_BACKEND="${SEARCH_BACKEND:-sqlite}"
  export FORCE_CPU_MODE="${FORCE_CPU_MODE:-true}"
  export USE_GPU="${USE_GPU:-false}"
  export TORCH_DEVICE="${TORCH_DEVICE:-cpu}"
  export COMPUTE_TYPE="${COMPUTE_TYPE:-int8}"
  export WHISPER_MODEL="${WHISPER_MODEL:-base}"
  export WHISPER_COMPUTE_TYPE="${WHISPER_COMPUTE_TYPE:-int8}"
  export ENABLE_DIARIZATION="${ENABLE_DIARIZATION:-false}"
  export FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"
}

need_backend_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing backend command: $1" >&2
    exit 1
  fi
}

check_port() {
  local label="$1" port="$2"
  if nc -z 127.0.0.1 "$port" >/dev/null 2>&1; then
    echo "$label:$port listening"
  else
    echo "$label:$port closed"
  fi
}

check() {
  load_env
  check_port postgres "${POSTGRES_PORT:-5432}"
  check_port redis "${REDIS_PORT:-6379}"
  check_port opensearch "${OPENSEARCH_PORT:-9200}"
  for tool in ffmpeg ffprobe psql redis-cli node npm; do
    command -v "$tool" >/dev/null 2>&1 && echo "$tool ok" || echo "$tool missing"
  done
}

api() {
  load_env
  cd "$BACKEND_DIR"
  exec "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port "${OPENTRANSCRIBE_API_PORT:-5174}"
}

migrate() {
  load_env
  cd "$BACKEND_DIR"
  exec "$PYTHON_BIN" -m alembic upgrade head
}

worker_cpu() {
  load_env
  cd "$BACKEND_DIR"
  if [[ "${LOCAL_ASR_BACKEND:-}" == "mlx-whisper" ]]; then
    exec "$PYTHON_BIN" -m celery -A app.core.celery worker --loglevel=info -Q cpu,utility,cpu-transcribe \
      --pool=solo --concurrency=1 \
      --hostname cpu-processor@%h -E
  else
    exec "$PYTHON_BIN" -m celery -A app.core.celery worker --loglevel=info -Q cpu,utility,cpu-transcribe \
      --concurrency="${CPU_WORKER_CONCURRENCY:-8}" --max-tasks-per-child=20 \
      --hostname cpu-processor@%h -E
  fi
}

worker_download() {
  load_env
  cd "$BACKEND_DIR"
  exec "$PYTHON_BIN" -m celery -A app.core.celery worker --loglevel=info -Q download \
    --concurrency="${DOWNLOAD_CONCURRENCY:-5}" --max-tasks-per-child=10 \
    --hostname media-downloader@%h -E
}

worker_nlp() {
  load_env
  cd "$BACKEND_DIR"
  exec "$PYTHON_BIN" -m celery -A app.core.celery worker --loglevel=info -Q nlp,celery \
    --concurrency="${NLP_WORKER_CONCURRENCY:-4}" --max-tasks-per-child=50 \
    --hostname ai-nlp@%h -E
}

worker_embedding() {
  load_env
  cd "$BACKEND_DIR"
  exec "$PYTHON_BIN" -m celery -A app.core.celery worker --loglevel=info -Q embedding \
    --concurrency=1 --max-tasks-per-child=500 --hostname search-indexer@%h -E
}

beat() {
  load_env
  cd "$BACKEND_DIR"
  exec "$PYTHON_BIN" -m celery -A app.core.celery beat --loglevel=info
}

frontend() {
  load_env
  cd "$FRONTEND_DIR"
  export VITE_API_PROXY_TARGET="${VITE_API_PROXY_TARGET:-http://127.0.0.1:5174}"
  export VITE_WS_PROXY_TARGET="${VITE_WS_PROXY_TARGET:-ws://127.0.0.1:5174}"
  exec npm run dev -- --host 127.0.0.1
}

usage() {
  cat <<'TXT'
Usage: scripts/native-dev.sh <command>

Commands: check, migrate, api, frontend, worker-cpu, worker-download,
          worker-nlp, worker-embedding, beat

This wrapper is loopback/no-container/no-CUDA oriented. It never starts or
stops Postgres, Redis, or OpenSearch for you; run check first and start those
host services yourself when ready.
TXT
}

case "${1:-help}" in
  check) check ;;
  migrate) migrate ;;
  api) api ;;
  frontend) frontend ;;
  worker-cpu) worker_cpu ;;
  worker-download) worker_download ;;
  worker-nlp) worker_nlp ;;
  worker-embedding) worker_embedding ;;
  beat) beat ;;
  help|--help|-h) usage ;;
  *) usage; exit 2 ;;
esac
