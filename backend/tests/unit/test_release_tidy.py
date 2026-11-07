"""`scripts/release-tidy.sh` (issue #1370): dry-run by default, and every deletion is one of
three registered shapes, each delegated to an existing scoped tool.

Runs the REAL script in a throwaway git repo with a FAKE `opentr.sh` (OT_TIDY_OPENTR) that
only logs its argv, so no Docker, stack or remote is ever touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TIDY_SH = REPO_ROOT / "scripts" / "release-tidy.sh"
GIT = shutil.which("git") or "git"

pytestmark = [
    pytest.mark.skipif(not TIDY_SH.exists(), reason="scripts/release-tidy.sh missing"),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

_FAKE_OPENTR = """#!/bin/bash
echo "$@" >> "$FAKE_LOG"
if [ "$1" = fresh-list ]; then
  echo "Fresh deployments:"
  echo "  • rel-0-7-0  (project otfresh-rel-0-7-0, 0 container(s) running, standard dev ports)"
  echo "  • mine  (project otfresh-mine, 2 container(s) running, standard dev ports)"
fi
exit 0
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy(TIDY_SH, r / "scripts" / "release-tidy.sh")
    _git(r, "init", "-q", "-b", "master")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("1")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "branch", "merged-one")
    _git(r, "branch", "release/0.6")
    _git(r, "checkout", "-q", "-b", "unmerged")
    (r / "f").write_text("2")
    _git(r, "commit", "-qam", "unmerged work")
    _git(r, "checkout", "-q", "master")
    return r


def _run(repo: Path, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    log = tmp_path / "fake.log"
    fake = tmp_path / "opentr.sh"
    fake.write_text(_FAKE_OPENTR)
    fake.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "OT_TIDY_OPENTR": str(fake),
        "FAKE_LOG": str(log),
        "TMPDIR": str(tmp_path / "scratch"),
    }
    (tmp_path / "scratch").mkdir(exist_ok=True)
    proc = subprocess.run(
        [str(repo / "scripts" / "release-tidy.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, log


def _branches(repo: Path) -> set[str]:
    return set(_git(repo, "branch", "--format=%(refname:short)").split())


@pytest.mark.unit
def test_without_yes_it_only_lists(repo: Path, tmp_path: Path) -> None:
    proc, log = _run(repo, tmp_path, "--project", "proj-x")

    assert proc.returncode == 0, proc.stderr
    assert "would delete branch: merged-one" in proc.stdout
    assert "would destroy fresh stack: rel-0-7-0" in proc.stdout
    assert {"merged-one", "unmerged", "release/0.6"} <= _branches(repo)
    calls = log.read_text().splitlines()
    assert not any(c.startswith("fresh-destroy") for c in calls), calls
    assert "test-cleanup --project proj-x --once --dry-run" in calls


@pytest.mark.unit
def test_yes_deletes_only_the_merged_branch(repo: Path, tmp_path: Path) -> None:
    proc, _ = _run(repo, tmp_path, "--yes")

    assert proc.returncode == 0, proc.stderr
    left = _branches(repo)
    assert "merged-one" not in left
    # unmerged work, the base, and release/* survive
    assert {"unmerged", "master", "release/0.6"} <= left


@pytest.mark.unit
def test_yes_destroys_only_rel_fresh_stacks(repo: Path, tmp_path: Path) -> None:
    proc, log = _run(repo, tmp_path, "--yes")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text().splitlines()
    assert "fresh-destroy rel-0-7-0 --yes" in calls
    assert not any("mine" in c and c.startswith("fresh-destroy") for c in calls), calls


@pytest.mark.unit
def test_yes_runs_test_cleanup_for_each_project_without_dry_run(repo: Path, tmp_path: Path) -> None:
    proc, log = _run(repo, tmp_path, "--yes", "--project", "a", "--project", "b")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text().splitlines()
    assert "test-cleanup --project a --once" in calls
    assert "test-cleanup --project b --once" in calls
    assert not any("--dry-run" in c for c in calls), calls


@pytest.mark.unit
def test_never_issues_a_prune_or_volume_removal() -> None:
    text = TIDY_SH.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "prune" not in code
    assert "--volumes" not in code
    assert "docker " not in code
