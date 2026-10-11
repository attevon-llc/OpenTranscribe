"""``--cleanup`` of a release rehearsal removes the LAST run's TEST_ROOT (issue #1195).

``test-{fresh-install,upgrade,lite-mode}.sh`` default ``TEST_ROOT`` to a fresh timestamp, so a
bare ``--cleanup`` used to point ``gr_cleanup`` at a directory that never existed: the run's
containers and volumes went, its TEST_ROOT tree (and the git worktree test-upgrade registers
inside it) stayed. ``gr_check_test_root`` now records the root it accepted and ``gr_cleanup``
uses that record when the caller's root was only a default.

The real ``lib/guardrails.sh`` runs here against a fake ``docker`` (prints nothing, so steps
1-3 find nothing to remove) and a throwaway git repository, entirely under ``tmp_path``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARDRAILS = _REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"
_BASH = shutil.which("bash") or "/bin/bash"
_GIT = shutil.which("git") or "/usr/bin/git"
_PROJECT = "ot-reltest-pytestroot"


def _guardrails(tmp_path: Path, test_root: Path, body: str, repo: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TEST_SCENARIO": "pytestroot",
        "TEST_PROJECT_NAME": _PROJECT,
        "TEST_ROOT": str(test_root),
        "TEST_LABEL": "com.opentranscribe.release-test=pytestroot",
        "TEST_PORTS": "",
        "REPO_ROOT": str(repo),
        "OT_RELEASE_TEST_ALLOW_PATH": "1",
        "GR_STOCK_PROJECT": "otpytestroot",
        "GR_OWNED_STAMP": str(tmp_path / "owned-stamp"),
    }
    return subprocess.run(  # noqa: S603 - fixed argv: bash sourcing the repo's guardrails
        [_BASH, "-c", f'source "{_GUARDRAILS}"; trap - EXIT; {body}'],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _repo_with_worktree_under(tmp_path: Path, root: Path) -> Path:
    repo = tmp_path / "repo"
    for argv in (
        ["init", "-q", str(repo)],
        [
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "seed",
        ],
        ["-C", str(repo), "worktree", "add", "-q", "--detach", str(root / "worktree-v0.0.1")],
    ):
        subprocess.run([_GIT, *argv], check=True, capture_output=True, timeout=60)  # noqa: S603
    return repo


@pytest.mark.unit
def test_a_defaulted_cleanup_removes_the_last_runs_root_and_its_worktree(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    last = runs / f"{_PROJECT}-20260101-000000"
    last.mkdir(parents=True)
    repo = _repo_with_worktree_under(tmp_path, last)

    accepted = _guardrails(tmp_path, last, "gr_check_test_root", repo)
    assert accepted.returncode == 0, accepted.stderr
    assert (runs / f".last-{_PROJECT}").read_text(encoding="utf-8").strip() == str(last)

    fresh_default = runs / f"{_PROJECT}-20260202-000000"  # what a bare --cleanup computes
    cleaned = _guardrails(tmp_path, fresh_default, "gr_cleanup 1", repo)
    assert not last.exists(), f"the last run's TEST_ROOT survived: {cleaned.stdout}{cleaned.stderr}"
    listing = subprocess.run(  # noqa: S603 - fixed argv
        [_GIT, "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    assert str(last) not in listing, "the worktree registration under TEST_ROOT survived"


@pytest.mark.unit
def test_an_explicit_root_is_never_swapped_for_the_last_one(tmp_path: Path) -> None:
    """test-upgrade.sh's per-hop cleanup passes its own TEST_ROOT; it must clean THAT."""
    runs = tmp_path / "runs"
    last = runs / f"{_PROJECT}-20260101-000000"
    explicit = runs / "hop-cleanup"
    last.mkdir(parents=True)
    explicit.mkdir()
    repo = tmp_path / "repo"
    subprocess.run([_GIT, "init", "-q", str(repo)], check=True, timeout=60)  # noqa: S603
    assert _guardrails(tmp_path, last, "gr_check_test_root", repo).returncode == 0

    _guardrails(tmp_path, explicit, "gr_cleanup 0", repo)
    assert last.exists(), "an explicit TEST_ROOT must not redirect cleanup to another run"
    assert not explicit.exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    "scenario", ["test-fresh-install.sh", "test-upgrade.sh", "test-lite-mode.sh"]
)
def test_each_scenario_tells_gr_cleanup_whether_its_root_was_defaulted(scenario: str) -> None:
    source = (_REPO_ROOT / "scripts" / "release-tests" / scenario).read_text(encoding="utf-8")
    assert re.search(r'^\[\[ -n "\$\{TEST_ROOT:-\}" \]\] \|\| TEST_ROOT_DEFAULTED=1$', source, re.M)
    calls = re.findall(r"^\s+gr_cleanup\b.*$", source, re.M)
    assert calls and all('"$TEST_ROOT_DEFAULTED"' in c for c in calls), calls
