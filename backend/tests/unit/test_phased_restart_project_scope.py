"""``perform_phased_restart`` watches THIS project's backend, through the compose chain.

It used to ``docker inspect`` / ``docker logs`` the literal container ``opentranscribe-backend``.
A ``--fresh`` stack renames its containers (``otfresh-<name>-backend``) through an overlay, so
an upgrade of an isolated stack read the live stack's backend: a dead fresh backend went
unnoticed for the full 15 minutes and the failure report printed another stack's logs.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_function
from tests.unit._shell_extract import requires_install_scripts
from tests.unit._shell_extract import run_shell

pytestmark = requires_install_scripts

CHAIN = "-f docker-compose.yml -f .fresh/pd7.yml"


def _run(tmp_path: Path, *, healthy: bool, state: str) -> tuple[str, list[str]]:
    calls = tmp_path / "docker.calls"
    snippet = f"""
RED=''; YELLOW=''; GREEN=''; BLUE=''; NC=''
read_env_value() {{ echo ""; }}
curl() {{ case "$*" in *"/health"*) {"return 0" if healthy else "return 7"} ;; esac; return 7; }}
sleep() {{ :; }}
docker() {{
  echo "$*" >> {calls}
  case "$*" in
    *" ps -a --format "*) echo "{state}" ;;
    *" logs --tail 40 backend") echo "FRESH_BACKEND_LOG" ;;
  esac
  return 0
}}
{extract_function(MANAGER, "perform_phased_restart")}
perform_phased_restart "{CHAIN}"
echo "RC:$?"
"""
    out = run_shell(snippet)
    return out, calls.read_text().splitlines() if calls.exists() else []


def test_a_dead_backend_is_found_through_the_chain_and_its_own_logs_are_shown(tmp_path: Path):
    """Must fire: the literal name inspected and printed the live stack's container."""
    out, calls = _run(tmp_path, healthy=False, state="exited:1")

    assert "RC:1" in out, out
    assert "exited with non-zero code: exited:1" in out, out
    assert "FRESH_BACKEND_LOG" in out, out
    assert f"compose {CHAIN} ps -a --format {{{{.State}}}}:{{{{.ExitCode}}}} backend" in calls
    assert not [c for c in calls if "opentranscribe-backend" in c], calls


def test_a_backend_that_never_turns_healthy_times_out_with_its_own_logs(tmp_path: Path):
    out, calls = _run(tmp_path, healthy=False, state="running:0")

    assert "RC:1" in out, out
    assert "failed to become healthy within 900s" in out, out
    assert f"compose {CHAIN} logs --tail 40 backend" in calls


def test_a_healthy_backend_starts_the_rest(tmp_path: Path):
    """Control: the happy path neither reports a failure nor dumps logs."""
    out, calls = _run(tmp_path, healthy=True, state="running:0")

    assert "RC:0" in out, out
    assert "Backend healthy after 0s" in out, out
    assert not [c for c in calls if " logs " in c], calls
    assert f"compose {CHAIN} up -d" in calls
