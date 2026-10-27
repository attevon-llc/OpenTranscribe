"""``scripts/build-all.sh`` must carry its version argument through, and report where it built.

Three defects, all found by reading the script against the two it drives:

1. The version never reached ``docker-build-push.sh``. That script takes the tag from the
   ``VERSION`` *environment variable*; ``build-all.sh`` held it in an unexported shell variable,
   so the pushed image tags silently came from the ``VERSION`` file whatever the operator typed.
2. ``build-offline-package.sh`` prefixes its own ``v``, so ``build-all.sh v2.1.0`` produced
   ``opentranscribe-offline-vv2.1.0``.
3. The closing summary looked in ``offline-package-build/`` while the package is written to
   ``offline-package-build/linux/``, so it listed nothing.

Hermetic: the two child scripts and every external tool are stubs in a scratch tree, so nothing
is built, pushed or pulled. The offline script's version handling is exercised by extracting
its real ``# Configuration`` block rather than a copy.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_ALL = REPO_ROOT / "scripts" / "build-all.sh"
OFFLINE = REPO_ROOT / "scripts" / "build-offline-package.sh"

pytestmark = pytest.mark.skipif(not BUILD_ALL.exists(), reason="scripts/build-all.sh not present")


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(BUILD_ALL, root / "scripts" / "build-all.sh")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "VERSION").write_text("v9.8.7\n")
    # The recorder: what VERSION did the push script actually receive?
    _exe(
        root / "scripts" / "docker-build-push.sh",
        'echo "push VERSION=${VERSION-<unset>}" >> "$(dirname "$0")/../calls.log"\n',
    )
    _exe(
        root / "scripts" / "build-offline-package.sh",
        'echo "offline ARG=$1" >> "$(dirname "$0")/../calls.log"\n'
        "mkdir -p offline-package-build/linux\n"
        'touch "offline-package-build/linux/opentranscribe-offline-v${1#v}.tar.xz"\n',
    )
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _exe(stubs / "docker", '[ "$1" = info ] && echo " Username: tester"; exit 0\n')
    _exe(stubs / "git", "echo abc1234\n")
    for tool in ("jq", "xz", "tar"):
        _exe(stubs / tool, "exit 0\n")
    return root


def _run(root: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-all.sh"), *args],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={
            "PATH": f"{root.parent / 'stubs'}:/usr/bin:/bin",
            "HUGGINGFACE_TOKEN": "hf_test",
            "HOME": str(root),
        },
        timeout=60,
    )
    log = root / "calls.log"
    return proc, (log.read_text().splitlines() if log.exists() else [])


def test_version_argument_reaches_both_phases(scratch: Path) -> None:
    proc, calls = _run(scratch, "v2.1.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == ["push VERSION=v2.1.0", "offline ARG=v2.1.0"]


def test_default_version_is_the_version_file_not_a_git_sha(scratch: Path) -> None:
    proc, calls = _run(scratch)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == ["push VERSION=v9.8.7", "offline ARG=v9.8.7"]


def test_default_without_version_file_is_an_accepted_dev_tag(scratch: Path) -> None:
    (scratch / "VERSION").unlink()
    proc, calls = _run(scratch)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls[0] == "push VERSION=dev-abc1234"


def test_summary_lists_the_package_from_the_linux_subdirectory(scratch: Path) -> None:
    proc, _ = _run(scratch, "2.1.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "opentranscribe-offline-v2.1.0.tar.xz" in proc.stdout
    assert "offline-package-build/linux/opentranscribe-offline-v2.1.0.tar.xz" in proc.stdout


@pytest.mark.parametrize("given", ["v2.1.0", "2.1.0"])
def test_offline_package_name_has_exactly_one_v(given: str) -> None:
    """Run the offline script's real version/name block, not a re-implementation of it."""
    lines = OFFLINE.read_text().splitlines()
    start = lines.index("# Configuration")
    end = next(i for i, ln in enumerate(lines) if ln.startswith("PACKAGE_NAME="))
    block = "\n".join(lines[start : end + 1])
    proc = subprocess.run(
        ["bash", "-c", f'VERSION="{given}"\n{block}\necho "$PACKAGE_NAME"'],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    assert proc.stdout.strip() == "opentranscribe-offline-v2.1.0", proc.stderr
