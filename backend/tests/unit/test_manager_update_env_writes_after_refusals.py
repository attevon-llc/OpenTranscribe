"""``opentranscribe.sh update`` must not touch .env until every refusal has had its say.

A refused ``update --rollback`` / ``--version`` used to leave ``OT_IMAGE_TAG`` pinned to the
target and the ``# OT_PREVIOUS_IMAGE_TAG`` rollback marker overwritten, so a plain restart
started old images against a newer schema and a re-run rolled the wrong way.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_functions

pytestmark = pytest.mark.unit

ENV_BEFORE = "ENVIRONMENT=production\nOT_IMAGE_TAG=v0.7.0\n# OT_PREVIOUS_IMAGE_TAG=v0.6.1\n"

_FUNCS = [
    "update_parse_args",
    "update_resolve_rollback_target",
    "update_refuse_unforced_downgrade",
    "sed_env_inplace",
    "append_env_line",
    "update_pin_image_tag",
    "update_check_rollback_compat",
    "run_upgrade_sequence",
    "cmd_update",
]


def _run(tmp_path: Path, *, args: str, preflight_rc: int, pull_rc: int, docker_run_rc: int):
    (tmp_path / ".env").write_text(ENV_BEFORE)
    script = f"""
GREEN=; YELLOW=; RED=; NC=
cd {tmp_path}
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
read_env_value() {{ grep "^$1=" .env | cut -d= -f2; }}
apply_deployment_pins() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
show_access_info() {{ :; }}
preflight_upgrade_env() {{ return {preflight_rc}; }}
compose_pull_for_upgrade() {{ echo "PULL tag=$OT_IMAGE_TAG"; return {pull_rc}; }}
compose_down_for_upgrade() {{ echo REACHED_TEARDOWN; echo down >> {tmp_path}/down.log; }}
perform_phased_restart() {{ echo REACHED_RESTART; }}
docker() {{
  if [ "$1" = "compose" ]; then echo some_head; return 0; fi
  if [ "$1" = "run" ]; then return {docker_run_rc}; fi
}}
{extract_functions(MANAGER, _FUNCS)}
cmd_update update {args}
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=60
    )


@pytest.mark.parametrize(
    ("args", "preflight_rc", "pull_rc", "docker_run_rc"),
    [
        ("--rollback", 0, 0, 1),  # rollback-compat refuses
        ("--version v0.8.0", 1, 0, 0),  # preflight refuses
        ("--version v0.8.0", 0, 1, 0),  # image pull fails
        ("--version v0.6.0", 0, 0, 0),  # unforced downgrade refused
    ],
)
def test_refused_update_leaves_env_byte_identical(
    tmp_path, args, preflight_rc, pull_rc, docker_run_rc
):
    result = _run(
        tmp_path,
        args=args,
        preflight_rc=preflight_rc,
        pull_rc=pull_rc,
        docker_run_rc=docker_run_rc,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (tmp_path / ".env").read_text() == ENV_BEFORE
    assert "REACHED_RESTART" not in result.stdout


@pytest.mark.parametrize("args", ["--version v0.8.0", "--rollback"])
def test_failed_pull_aborts_before_the_old_stack_is_taken_down(tmp_path, args):
    result = _run(tmp_path, args=args, preflight_rc=0, pull_rc=1, docker_run_rc=0)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (tmp_path / "down.log").exists()
    assert (tmp_path / ".env").read_text() == ENV_BEFORE


def test_committed_update_pulls_target_tag_then_writes_tag_and_marker(tmp_path):
    result = _run(tmp_path, args="--version v0.8.0", preflight_rc=0, pull_rc=0, docker_run_rc=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PULL tag=v0.8.0" in result.stdout
    env = (tmp_path / ".env").read_text()
    assert "OT_IMAGE_TAG=v0.8.0\n" in env
    assert "# OT_PREVIOUS_IMAGE_TAG=v0.7.0\n" in env
    assert "REACHED_TEARDOWN" in result.stdout
    assert "REACHED_RESTART" in result.stdout
