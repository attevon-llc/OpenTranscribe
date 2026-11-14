"""Which tags gain a ``v`` prefix: only a bare release number, never ``latest`` or a local tag.

``opentranscribe.sh update`` prefixed ``v`` to any target not starting with one, and
``--rollback`` feeds it the recorded previous tag. A recorded ``latest`` therefore rolled back
to ``vlatest`` and a local ``otfresh-<name>`` build to ``votfresh-<name>``: images that do not
exist. ``setup-opentranscribe.sh --version`` had the same rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._shell_extract import INSTALLER
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_function
from tests.unit._shell_extract import extract_functions
from tests.unit._shell_extract import requires_install_scripts
from tests.unit._shell_extract import run_shell

pytestmark = requires_install_scripts

_UPDATE_FUNCTIONS = [
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


def _pinned_tag_after_update(tmp_path: Path, env: str, args: str) -> str:
    """Run the REAL `update` command with every docker-facing step stubbed; return the pinned tag."""
    (tmp_path / ".env").write_text(env)
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
cd {tmp_path}
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
apply_deployment_pins() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
preflight_upgrade_env() {{ :; }}
compose_pull_for_upgrade() {{ :; }}
compose_down_for_upgrade() {{ :; }}
perform_phased_restart() {{ :; }}
show_access_info() {{ :; }}
docker() {{ return 0; }}
set -- update {args}
{extract_functions(MANAGER, _UPDATE_FUNCTIONS)}
cmd_update "$@"
grep '^OT_IMAGE_TAG=' .env
"""
    out = run_shell(snippet)
    pinned = [line for line in out.splitlines() if line.startswith("OT_IMAGE_TAG=")]
    assert pinned, out
    return pinned[-1].removeprefix("OT_IMAGE_TAG=")


@pytest.mark.parametrize("previous", ["latest", "otfresh-proddemo7"])
def test_rollback_returns_to_a_non_version_tag_as_written(tmp_path: Path, previous: str):
    """Must fire: `vlatest` / `votfresh-proddemo7` were pinned and the rollback pulled nothing."""
    env = f"OT_IMAGE_TAG=v0.7.0\n# OT_PREVIOUS_IMAGE_TAG={previous}\n"

    assert _pinned_tag_after_update(tmp_path, env, "--rollback") == previous


def test_rollback_to_a_release_keeps_its_v(tmp_path: Path):
    env = "OT_IMAGE_TAG=v0.7.0\n# OT_PREVIOUS_IMAGE_TAG=v0.6.1\n"

    assert _pinned_tag_after_update(tmp_path, env, "--rollback") == "v0.6.1"


def test_a_bare_release_number_still_gains_its_v(tmp_path: Path):
    """Control: the normalisation the prefix exists for still happens."""
    assert (
        _pinned_tag_after_update(tmp_path, "OT_IMAGE_TAG=v0.6.1\n", "--version 0.7.0") == "v0.7.0"
    )


@pytest.mark.parametrize(
    ("given", "pinned"), [("0.7.0", "v0.7.0"), ("v0.7.0", "v0.7.0"), ("latest", "latest")]
)
def test_installer_version_gains_a_v_only_when_it_is_a_bare_number(given: str, pinned: str):
    snippet = (
        extract_function(INSTALLER, "resolve_install_ref")
        + f"""
print_info(){{ :; }}; print_success(){{ :; }}; print_warning(){{ :; }}; print_error(){{ :; }}
OPENTRANSCRIBE_VERSION="{given}"; OPENTRANSCRIBE_BRANCH=""
resolve_install_ref
echo "RESULT:$OPENTRANSCRIBE_BRANCH|$OT_IMAGE_TAG"
"""
    )
    assert run_shell(snippet).endswith(f"RESULT:{pinned}|{pinned}")
