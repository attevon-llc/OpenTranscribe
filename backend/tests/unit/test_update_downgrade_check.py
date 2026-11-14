"""``update --version``'s downgrade refusal orders two RELEASE versions and nothing else.

A non-release current tag (``latest``, a local ``otfresh-<name>`` build, a ``dev-<sha>`` tag)
has no order. ``sort -V`` ranked ``otfresh-proddemo7`` below every ``vX.Y.Z``, so moving an
isolated stack from its local build to a release-shaped tag was refused as a downgrade. The
check now says it does not apply, as it always silently did for ``latest``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_functions
from tests.unit._shell_extract import requires_install_scripts
from tests.unit._shell_extract import run_shell

pytestmark = requires_install_scripts

_UPDATE_FUNCTIONS = [
    "read_env_value",
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


def _update(tmp_path: Path, current: str, target: str) -> tuple[str, str]:
    """Run the REAL `update --version <target>` with docker stubbed; return (output, pinned tag)."""
    (tmp_path / ".env").write_text(f"OT_IMAGE_TAG={current}\n")
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
set -- update --version {target}
{extract_functions(MANAGER, _UPDATE_FUNCTIONS)}
cmd_update "$@"
"""
    out = run_shell(snippet)
    pinned = (tmp_path / ".env").read_text().split("OT_IMAGE_TAG=")[-1].splitlines()[0]
    return out, pinned


@pytest.mark.parametrize("current", ["otfresh-proddemo7", "dev-1a2b3c4", "latest"])
def test_a_non_release_current_tag_is_not_called_a_downgrade(tmp_path: Path, current: str):
    """Must fire for the local tags: they were refused as a downgrade, nothing was pinned."""
    out, pinned = _update(tmp_path, current, "v0.7.0-demo.1")

    assert "Refusing to downgrade" not in out, out
    assert f"{current} → v0.7.0-demo.1: not two release versions, so no downgrade check" in out
    assert pinned == "v0.7.0-demo.1"


def test_a_non_release_target_is_not_ordered_either(tmp_path: Path):
    out, pinned = _update(tmp_path, "v0.7.0", "otfresh-proddemo7")

    assert "Refusing to downgrade" not in out, out
    assert "not two release versions" in out, out
    assert pinned == "otfresh-proddemo7"


def test_a_real_downgrade_between_releases_is_still_refused(tmp_path: Path):
    """Control: the refusal itself still fires, and nothing is pinned."""
    out, pinned = _update(tmp_path, "v0.7.0", "v0.6.1")

    assert "Refusing to downgrade v0.7.0 → v0.6.1" in out, out
    assert pinned == "v0.7.0"


def test_an_upgrade_between_releases_proceeds_without_the_notice(tmp_path: Path):
    out, pinned = _update(tmp_path, "v0.6.1", "v0.7.0-rc.1")

    assert "Refusing to downgrade" not in out, out
    assert "not two release versions" not in out, out
    assert pinned == "v0.7.0-rc.1"
