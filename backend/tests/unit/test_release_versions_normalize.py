"""``ver_normalize`` gives a release number its ``v`` and leaves every other tag as written.

``scripts/release-tests/test-lite-mode.sh`` and ``test-cold-model-download.sh`` pass an
operator's ``LOCAL_IMAGE_TAG`` through it. It used to prefix ``v`` to anything, so a local
``otfresh-<name>`` build became ``votfresh-<name>`` and ``latest`` became ``vlatest``: tags no
image carries.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_SH = REPO_ROOT / "scripts" / "release-tests" / "lib" / "versions.sh"

pytestmark = pytest.mark.skipif(
    not VERSIONS_SH.is_file() or shutil.which("bash") is None,
    reason="versions.sh or bash is not present in this checkout",
)


def _normalize(value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'REPO_ROOT="{REPO_ROOT}" source "{VERSIONS_SH}"; ver_normalize "$1"',
            "_",
            value,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("0.7.0", "v0.7.0"),
        ("v0.7.0", "v0.7.0"),
        ("0.7.0-rc.1", "v0.7.0-rc.1"),
        ("otfresh-proddemo7", "otfresh-proddemo7"),
        ("latest", "latest"),
    ],
)
def test_only_a_release_number_gains_a_v(given: str, expected: str):
    proc = _normalize(given)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


@pytest.mark.parametrize("given", ["", "v"])
def test_an_empty_version_is_refused(given: str):
    assert _normalize(given).returncode == 1
