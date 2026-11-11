#!/usr/bin/env bash
# Subcommand target resolution for opentr.sh: which deployment a command acts on.
# Sourced by opentr.sh (not executable on its own). Relies on restart_resolve_target and
# restart_compose, defined in opentr.sh, at call time.

# `backup|restore [--fresh <name>] ...` — run backup_database / restore_database (common.sh)
# against the deployment asked for. Without --fresh: compose chain "" = bare `docker compose`,
# which in a repo clone auto-loads docker-compose.override.yml (issue #613, unchanged). With
# --fresh: the project name and chain come from restart_resolve_target (the same resolver
# start/stop/status use), and COMPOSE_PROJECT_NAME is exported so every docker call in
# common.sh -- including ones that take no chain -- is pinned to that project. A restore
# DROPs the database, so an unnamed fall-through to the live stack is the failure to avoid.
# Other flags are left to the common.sh functions, which reject unknown ones.
backup_restore_run() {
  local action="$1"
  shift
  local fresh_args=() rest=()
  while [ $# -gt 0 ]; do
    if [ "$1" = "--fresh" ]; then
      fresh_args=(--fresh "${2:-}")
      shift
      [ $# -gt 0 ] && shift
    else
      rest+=("$1")
      shift
    fi
  done
  restart_resolve_target ${fresh_args[@]+"${fresh_args[@]}"} || return $?
  if [ -n "$RESTART_PROJECT" ]; then
    export COMPOSE_PROJECT_NAME="$RESTART_PROJECT"
    echo "🧪 ${action} targets ${RESTART_LABEL}"
  fi
  if [ "$action" = "backup" ]; then
    # backup_database reads only $1 and silently ignored the rest; refuse extras instead.
    if [ ${#rest[@]} -gt 1 ]; then
      echo "❌ Error: unknown backup option: ${rest[1]}" >&2
      echo "Usage: ./opentr.sh backup [--fresh <name>] [--encrypt]" >&2
      return 2
    fi
    backup_database "$RESTART_CHAIN" "./opentr.sh" "${rest[0]:-}"
  else
    restore_database "$RESTART_CHAIN" "./opentr.sh" ${rest[@]+"${rest[@]}"}
  fi
}

# Fail closed for subcommands that act on the default stack only (remove, purge, health,
# healthcheck-all [--json], build): any argument is refused, and `--fresh` gets a pointer to
# the command that does work on a fresh deployment. They used to ignore every argument, so
# `remove --fresh test1` prompted and then wiped the LIVE stack's volumes.
# Usage: reject_args <subcommand> [allowed-flag...] -- "$@"
reject_args() {
  local cmd="$1"
  shift
  local allowed=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do
    allowed+=("$1")
    shift
  done
  shift || true
  local arg ok a
  for arg in "$@"; do
    ok=false
    for a in ${allowed[@]+"${allowed[@]}"}; do [ "$arg" = "$a" ] && ok=true; done
    [ "$ok" = true ] && continue
    if [ "$arg" = "--fresh" ]; then
      case "$cmd" in
        remove | purge)
          echo "❌ $cmd does not accept --fresh and acts on the DEFAULT (live) stack only." >&2
          echo "   To delete a fresh deployment's containers and volumes: ./opentr.sh fresh-destroy <name>" >&2
          ;;
        *)
          echo "❌ $cmd does not accept --fresh; it acts on the default stack only." >&2
          echo "   Fresh deployments: ./opentr.sh status|logs|shell|backup|restore --fresh <name> (see fresh-list)." >&2
          ;;
      esac
    else
      echo "❌ $cmd: unexpected argument '$arg' (this command takes no arguments${allowed[*]:+ except ${allowed[*]}})." >&2
    fi
    return 2
  done
  return 0
}

# `logs|shell [service] [--fresh <name>]` -- positional service unchanged; --fresh resolved by
# restart_resolve_target like every other fresh-aware subcommand.
service_cmd_run() {
  local action="$1"
  shift
  local fresh_args=() svc="" arg
  while [ $# -gt 0 ]; do
    arg="$1"
    shift
    case "$arg" in
      --fresh)
        fresh_args=(--fresh "${1:-}")
        [ $# -gt 0 ] && shift
        ;;
      -*)
        echo "❌ $action: unknown option '$arg' (usage: ./opentr.sh $action [service] [--fresh <name>])" >&2
        return 2
        ;;
      *)
        if [ -n "$svc" ]; then
          echo "❌ $action: unexpected argument '$arg' (only one service)" >&2
          return 2
        fi
        svc="$arg"
        ;;
    esac
  done
  restart_resolve_target ${fresh_args[@]+"${fresh_args[@]}"} || return $?
  if [ "$action" = "logs" ]; then
    if [ -z "$svc" ]; then
      echo "📋 Showing logs for all services on ${RESTART_LABEL}... (press Ctrl+C to exit)"
      restart_compose logs -f
    else
      echo "📋 Showing logs for $svc on ${RESTART_LABEL}... (press Ctrl+C to exit)"
      restart_compose logs -f "$svc"
    fi
    return
  fi
  svc="${svc:-backend}"
  echo "🔧 Opening shell in $svc container on ${RESTART_LABEL}..."
  # Pick the shell first: `bash || sh` also started sh whenever the user left bash
  # with a non-zero status (the last command they ran failed).
  if restart_compose exec -T "$svc" sh -c 'command -v bash' >/dev/null 2>&1; then
    restart_compose exec "$svc" /bin/bash
  else
    restart_compose exec "$svc" /bin/sh
  fi
}
