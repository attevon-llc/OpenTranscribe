#!/usr/bin/env bash
# Service list and `--only` handling for `./opentr.sh restart-backend`.
# Sourced by opentr.sh (kept out of it: opentr.sh's file-size baseline may only go down).
# `docker compose restart [SERVICE...]` restarts only the named services:
# https://docs.docker.com/reference/cli/docker/compose/restart/

# The services `restart-backend` restarts when --only is not given (order is the compose order).
restart_backend_services() {
  echo "backend celery-worker celery-download-worker celery-cpu-worker celery-redaction" \
    "celery-cloud-asr-worker celery-nlp-worker celery-embedding-worker celery-beat flower" \
    "celery-worker-gpu-scaled"
}

# restart_parse_only <args...>: sets RESTART_ONLY (space-separated, empty = all) and
# RESTART_REST (array, the args with --only removed). Returns 2 on a bad or unknown name.
restart_parse_only() {
  RESTART_ONLY=""
  RESTART_REST=()
  local _arg _valid _name _known
  _valid="$(restart_backend_services)"
  while [ $# -gt 0 ]; do
    _arg="$1"
    shift
    if [ "$_arg" != "--only" ]; then
      RESTART_REST+=("$_arg")
      continue
    fi
    if [ $# -eq 0 ] || [ -z "${1:-}" ]; then
      echo "❌ --only needs a comma-separated service list. Valid: ${_valid}" >&2
      return 2
    fi
    for _name in $(echo "$1" | tr ',' ' '); do
      _known=0
      case " ${_valid} " in *" ${_name} "*) _known=1 ;; esac
      if [ "$_known" -ne 1 ]; then
        echo "❌ Unknown service for --only: ${_name}. Valid: ${_valid}" >&2
        return 2
      fi
      RESTART_ONLY="${RESTART_ONLY:+${RESTART_ONLY} }${_name}"
    done
    shift
  done
  return 0
}

# Restart backend, workers, beat and flower (or just --only). Needs restart_resolve_target and
# restart_compose from opentr.sh, which are defined before this runs.
restart_backend() {
  restart_parse_only "$@" || return $?
  restart_resolve_target "${RESTART_REST[@]+"${RESTART_REST[@]}"}" || return $?
  local _svcs="${RESTART_ONLY:-$(restart_backend_services)}"
  echo "🔄 Restarting ${RESTART_ONLY:-backend services (backend, all celery workers, celery-beat, flower)} on ${RESTART_LABEL}..."

  # -t "$OT_STOP_GRACE_GPU" (issue #782): compose v2.29.7's `restart` passes only
  # options.Timeout, so a container created before docker-compose.yml carried
  # stop_grace_period (StopTimeout still null) needs it spelled out here too.
  local rc=0 _required=() _s
  for _s in $_svcs; do
    [ "$_s" = "celery-worker-gpu-scaled" ] || _required+=("$_s")
  done
  if [ "${#_required[@]}" -gt 0 ]; then
    restart_compose restart -t "$OT_STOP_GRACE_GPU" "${_required[@]}" || rc=$?
  fi

  # celery-worker-gpu-scaled is optional (scale: 0 unless --gpu-scale), so its
  # absence is genuinely not an error — unlike everything above.
  case " ${_svcs} " in
    *" celery-worker-gpu-scaled "*)
      restart_compose restart -t "$OT_STOP_GRACE_GPU" celery-worker-gpu-scaled 2>/dev/null || true
      ;;
  esac

  if [ "$rc" -ne 0 ]; then
    echo "❌ Backend restart FAILED on ${RESTART_LABEL} (docker compose exit ${rc})." >&2
    echo "   Nothing above was necessarily restarted — do not read this as a success." >&2
    return "$rc"
  fi
  echo "✅ Backend services restarted successfully on ${RESTART_LABEL}."

  # Display container status
  echo "📊 Container status:"
  restart_compose ps
}
