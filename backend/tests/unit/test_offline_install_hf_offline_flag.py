"""The offline installers must actually turn ``HF_HUB_OFFLINE`` on in the ``.env`` they write.

``.env.example`` ships the key ACTIVE (``HF_HUB_OFFLINE=0``). Both installers flipped it with
``sed "s|^#.*HF_HUB_OFFLINE=.*|HF_HUB_OFFLINE=1|"`` -- a pattern that only matches a COMMENTED
line, so it matched nothing and every install kept ``0`` while printing "Offline mode enabled
(HF_HUB_OFFLINE=1)". The visible symptom was two contradictory token messages: the installer said
no ``HUGGINGFACE_TOKEN`` is needed, then ``opentr.sh`` warned that diarization would not work
without one (it keyed off the ``.env`` value the installer had failed to set).

The pattern is lifted out of each shipped script and applied to the real ``.env.example``, so this
fails if either the pattern or the template drifts.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SCRIPTS = [
    REPO_ROOT / "scripts" / "install-offline-package.sh",
    REPO_ROOT / "scripts" / "build-windows-installer.sh",
]
SED_RE = re.compile(r'sed -i "(s\|[^"\n]*HF_HUB_OFFLINE[^"\n]*)"')


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_installer_sed_flips_the_shipped_env_example(script: Path, tmp_path: Path) -> None:
    match = SED_RE.search(script.read_text())
    assert match, f"{script.name}: no HF_HUB_OFFLINE sed expression found"
    env = tmp_path / ".env"
    shutil.copy(ENV_EXAMPLE, env)
    assert re.search(r"^HF_HUB_OFFLINE=0$", env.read_text(), re.M), (
        ".env.example no longer ships an active HF_HUB_OFFLINE=0; update this test"
    )
    subprocess.run(["sed", "-i", match.group(1), str(env)], check=True)
    lines = [ln for ln in env.read_text().splitlines() if ln.startswith("HF_HUB_OFFLINE=")]
    assert lines == ["HF_HUB_OFFLINE=1"]


def test_installer_banner_does_not_demand_a_token() -> None:
    text = (REPO_ROOT / "scripts" / "install-offline-package.sh").read_text()
    assert "REQUIRED: Set your HUGGINGFACE_TOKEN" not in text


def test_offline_wrapper_does_not_warn_about_a_missing_token() -> None:
    text = (REPO_ROOT / "scripts" / "opentr-offline.sh").read_text()
    assert 'print_warning "HUGGINGFACE_TOKEN is not set' not in text
