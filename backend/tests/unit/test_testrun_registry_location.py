"""One liveness registry for every writer and the sweep (issue #1195).

Three things resolve where live test runs register themselves: ``scripts/testrun-registry.sh``
(the gate scripts), ``backend/tests/testrun_marker.py`` (every pytest process, via conftest)
and ``scripts/cleanup-test-data.py`` (the sweep reading them). If any of them disagrees, the
sweep cannot see a live run and deletes its throwaway stores mid-use — so all three are
asked, for real, and compared. The directory is the MAIN checkout's, shared by worktrees: a
marker written in one worktree must be visible to a sweep started from another.

The second half pins what this module's conftest change does: a pytest process registers a
live marker for its lifetime, and removes the ``/tmp`` tree it creates when it exits.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BASH = shutil.which("bash") or "/bin/bash"
_GIT = shutil.which("git") or "/usr/bin/git"


def _env_without_override() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "TESTRUN_REGISTRY_DIR"}


def _bash_dir() -> Path:
    out = subprocess.run(  # noqa: S603 - fixed argv: bash sourcing the repo's own registry
        [
            _BASH,
            "-c",
            f'source "{_REPO_ROOT}/scripts/testrun-registry.sh"; printf %s "$_TESTRUN_REGISTRY_DIR"',
        ],
        env=_env_without_override(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return Path(out.stdout)


def _sweep_dir() -> Path:
    out = subprocess.run(  # noqa: S603 - fixed argv: this interpreter importing the sweep
        [
            sys.executable,
            "-c",
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('c', '{_REPO_ROOT}/scripts/cleanup-test-data.py');"
            "m = importlib.util.module_from_spec(spec); sys.modules['c'] = m;"
            "spec.loader.exec_module(m); print(m._TESTRUNS_DIR, end='')",
        ],
        env=_env_without_override(),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return Path(out.stdout)


def _main_checkout() -> Path:
    common = subprocess.run(  # noqa: S603 - fixed argv
        [_GIT, "-C", str(_REPO_ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    return Path(common).parent


def _marker_module():
    spec = importlib.util.spec_from_file_location(
        "testrun_marker_under_test", _REPO_ROOT / "backend" / "tests" / "testrun_marker.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_bash_registry_pytest_marker_and_sweep_resolve_one_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TESTRUN_REGISTRY_DIR", raising=False)
    expected = _main_checkout() / ".testruns"
    assert _bash_dir() == expected
    assert _sweep_dir() == expected
    assert _marker_module().default_testruns_dir(_REPO_ROOT) == expected


@pytest.mark.unit
def test_the_override_wins_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESTRUN_REGISTRY_DIR", str(tmp_path))
    assert _marker_module().default_testruns_dir(_REPO_ROOT) == tmp_path


def _conftest_module():
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if path.endswith(os.path.join("backend", "tests", "conftest.py")):
            return module
    raise AssertionError("backend/tests/conftest.py is not loaded in this pytest process")


def _load_sweep():
    spec = importlib.util.spec_from_file_location(
        "cleanup_test_data_for_marker", _REPO_ROOT / "scripts" / "cleanup-test-data.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_a_pytest_controller_is_a_live_run_until_it_unconfigures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest = _conftest_module()
    sweep = _load_sweep()
    registry = tmp_path / ".testruns"
    monkeypatch.setenv("TESTRUN_REGISTRY_DIR", str(registry))
    config = SimpleNamespace(stash={})
    conftest._register_live_test_run(config)
    try:
        assert len(sweep.live_marker_start_times(registry)) == 1, "the run must read as live"
    finally:
        conftest.pytest_unconfigure(config)
    assert sweep.live_marker_start_times(registry) == []
    assert list(registry.glob("*.lock")) == [], "a finished run leaves no marker file"


@pytest.mark.unit
def test_an_xdist_worker_registers_no_marker_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest = _conftest_module()
    registry = tmp_path / ".testruns"
    monkeypatch.setenv("TESTRUN_REGISTRY_DIR", str(registry))
    config = SimpleNamespace(stash={}, workerinput={"workerid": "gw0"})
    conftest._register_live_test_run(config)
    conftest.pytest_unconfigure(config)
    assert not registry.exists() or list(registry.glob("*.lock")) == []


@pytest.mark.unit
def test_importing_the_conftest_leaves_no_tmp_tree_behind(tmp_path: Path) -> None:
    """Every pytest process (each xdist worker) used to leave one opentranscribe_test_* tree:
    7,358 of them were in /tmp on 2026-10-11."""
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {
        **_env_without_override(),
        "TMPDIR": str(scratch),
        "TESTRUN_REGISTRY_DIR": str(tmp_path / "r"),
    }
    subprocess.run(  # noqa: S603 - fixed argv: this interpreter executing the conftest
        [
            sys.executable,
            "-c",
            "import runpy, glob, os, sys;"
            f"runpy.run_path('{_REPO_ROOT}/backend/tests/conftest.py');"
            "made = glob.glob(os.path.join(os.environ['TMPDIR'], 'opentranscribe_test_*'));"
            "sys.exit(0 if made else 9)",
        ],
        cwd=str(_REPO_ROOT / "backend"),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,  # exit 9 = the conftest created nothing, so this test would prove nothing
    )
    assert list(scratch.glob("opentranscribe_test_*")) == []
