"""``update --version``'s downgrade refusal orders two RELEASE versions and nothing else.

A non-release current tag (``latest``, a local ``otfresh-<name>`` build, a ``dev-<sha>`` tag)
has no order. ``sort -V`` ranked ``otfresh-proddemo7`` below every ``vX.Y.Z``, so moving an
isolated stack from its local build to a release-shaped tag was refused as a downgrade. The
check now says it does not apply, as it always silently did for ``latest``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._shell_extract import requires_install_scripts
from tests.unit.manager_update_stub import run_update

pytestmark = requires_install_scripts


def _update(tmp_path: Path, current: str, target: str) -> tuple[str, str]:
    return run_update(tmp_path, f"OT_IMAGE_TAG={current}\n", f"--version {target}")


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
