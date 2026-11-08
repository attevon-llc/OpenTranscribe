"""``compose_run_migrate`` (scripts/common.sh): the migrate step for paths that skip ``depends_on``.

``docker compose start`` and ``up --no-deps`` never run the one-shot ``migrate`` service, and
an API started with ``RUN_MIGRATIONS_ON_STARTUP=false`` over an un-migrated database answers
503 forever. ``restore_database`` restarts the app with ``compose start``, so on a
``--migrate-forward`` restore nothing would migrate. These drive the REAL shell function with
only ``docker`` replaced.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit._shell_extract import COMMON
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_function
from tests.unit._shell_extract import requires_install_scripts
from tests.unit._shell_extract import run_shell
from tests.unit.test_upgrade_backup_restore_arms import _FAKE_DOCKER
from tests.unit.test_upgrade_backup_restore_arms import _FAKE_DUMP
from tests.unit.test_upgrade_backup_restore_arms import _backup_restore_arm

pytestmark = requires_install_scripts


def _fake_docker(services: str, run_rc: int) -> str:
    return f"""
docker() {{
  echo "DOCKER:$*" >&2
  [ "$1" = "compose" ] || return 0
  shift
  while [ "$1" = "-f" ]; do shift 2; done
  case "$1" in
    config) printf '%b' '{services}' ;;
    run) echo "RAN:$*"; return {run_rc} ;;
  esac
  return 0
}}
"""


def _call(services: str, run_rc: int = 0) -> str:
    snippet = f"""
{_fake_docker(services, run_rc)}
source {COMMON}
compose_run_migrate "-f docker-compose.yml -f docker-compose.prod.yml"
echo "RC:$?"
"""
    return run_shell(snippet)


def test_runs_the_one_shot_without_its_dependencies_when_the_chain_defines_it():
    out = _call("postgres\\nmigrate\\nbackend\\n")
    assert "RAN:run --rm --no-deps migrate" in out, out
    assert "RC:0" in out


def test_propagates_the_migrate_steps_exit_code():
    out = _call("migrate\\nbackend\\n", run_rc=3)
    assert "RC:3" in out, out
    assert "migration lock" in out


def test_is_a_no_op_for_compose_files_from_before_the_migrate_service():
    """Old compose files: the API still migrates itself, so there is nothing to run."""
    out = _call("postgres\\nbackend\\n")
    assert "RAN:" not in out, out
    assert "RC:0" in out


def test_a_service_merely_named_like_migrate_does_not_count():
    out = _call("migrate-legacy\\nbackend\\n")
    assert "RAN:" not in out, out


def _restore(tmp_path: Path, run_rc: int) -> str:
    """A full ``--yes`` restore (matching head) through the real arm and restore_database."""
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    snippet = f"""
{_FAKE_DOCKER}
eval "$(declare -f docker | sed '1s/^docker/_restore_docker/')"
docker() {{
  local a=("$@")
  if [ "$1" = "compose" ]; then
    local i=1
    while [ "${{a[$i]}}" = "-f" ]; do i=$((i + 2)); done
    case "${{a[$i]}}" in
      config) printf 'migrate\\nbackend\\n'; return 0 ;;
      run) echo "RAN_MIGRATE"; return {run_rc} ;;
      start) echo "STARTED_APP"; return 0 ;;
    esac
  fi
  _restore_docker "$@"
}}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml -f docker-compose.prod.yml"; }}
set -- restore --yes {dump}
{_backup_restore_arm()}
"""
    return run_shell(snippet)


def test_restore_migrates_before_restarting_the_app(tmp_path: Path):
    out = _restore(tmp_path, run_rc=0)
    assert "RAN_MIGRATE" in out and "STARTED_APP" in out, out
    assert out.index("RAN_MIGRATE") < out.index("STARTED_APP")


def test_restore_leaves_the_app_stopped_when_the_migration_fails(tmp_path: Path):
    out = _restore(tmp_path, run_rc=1)
    assert "RAN_MIGRATE" in out, out
    assert "STARTED_APP" not in out, "a failed migration must not fall through to start"
    assert "left STOPPED" in out


def test_an_upgrade_whose_migration_fails_stops_at_once_and_shows_the_migrate_log():
    """opentranscribe.sh's perform_phased_restart: `up ... backend` fails when migrate does.

    Before, the exit status of that `up` was ignored and the function polled /health for 15
    minutes against a backend container compose had never created.
    """
    body = extract_function(MANAGER, "perform_phased_restart")
    snippet = f"""
RED=''; YELLOW=''; GREEN=''; BLUE=''; NC=''
read_env_value() {{ echo ""; }}
curl() {{ echo "CURL_POLLED"; return 7; }}
sleep() {{ :; }}
docker() {{
  local a=("$@")
  if [ "$1" = "compose" ]; then
    local i=1
    while [ "${{a[$i]}}" = "-f" ]; do i=$((i + 2)); done
    case "${{a[$i]}}" in
      up) echo "service migrate didn't complete successfully: exit 1" >&2; return 1 ;;
      config) printf 'migrate\\nbackend\\n'; return 0 ;;
      logs) echo "MIGRATE_LOG ${{a[*]}}"; return 0 ;;
    esac
  fi
  return 0
}}
{body}
perform_phased_restart "-f docker-compose.yml -f docker-compose.prod.yml"
echo "RC:$?"
"""
    out = run_shell(snippet)
    assert "RC:1" in out, out
    assert "CURL_POLLED" not in out, "must not poll a backend that was never created"
    assert "MIGRATE_LOG" in out and "migrate" in out.split("MIGRATE_LOG", 1)[1]
