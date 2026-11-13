"""L1: build/scan/rehearse evidence survives a test-only fix, but only with proof.

`70-tag.sh`'s `artifacts-match-tagged-commit` block is extracted from the real script and run in a
scratch repo against the REAL policy file, with a fake `docker` reporting image IDs. Reuse needs ALL
of: every changed path is neutral or image-free (`evidence_reuse_paths`), every leg's image ID
equals the one build recorded (legs.tsv), and scripts/release-tests/** is byte-identical. The test
stage is never reusable (backend/tests/** is not neutral for it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"
TAG_SH = RELEASE_DIR / "70-tag.sh"
CRITERIA = RELEASE_DIR / "release-criteria.yaml"
GIT = shutil.which("git") or "git"
VERSION = "v9.9.9"
LEG_TAG = "davidamacey/opentranscribe-backend:v9.9.9-cuda-amd64"
IMAGE_ID = "sha256:aaaa"

pytestmark = [pytest.mark.unit, pytest.mark.skipif(shutil.which("git") is None, reason="needs git")]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _block() -> str:
    text = TAG_SH.read_text(encoding="utf-8")
    start = text.index("# --- BEGIN artifacts-match-tagged-commit ---")
    end = text.index("# --- END artifacts-match-tagged-commit ---", start)
    return text[start : text.index("\n", end) + 1]


def _commit(repo: Path, *rel_paths: str) -> None:
    for rel in rel_paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(target.read_text() + "x" if target.exists() else "x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delta")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text(".release/\n")
    (r / "scripts/release-tests").mkdir(parents=True)
    (r / "scripts/release-tests/t.sh").write_text("1")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    base = _git(r, "rev-parse", "--short", "HEAD")
    steps = r / ".release" / VERSION / "steps"
    steps.mkdir(parents=True)
    for stage in ("test", "build", "scan", "rehearse"):
        (steps / stage).write_text(f"status=done\nsha={base}\n")
    (r / ".release" / VERSION / "legs.tsv").write_text(
        f"backend\tdavidamacey/opentranscribe-backend\tlinux/amd64\t{LEG_TAG}\t{IMAGE_ID}\n"
    )
    return r


def _run(repo: Path, local_image_id: str = IMAGE_ID) -> tuple[int, str]:
    bindir = repo.parent / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(f"#!/bin/bash\necho '{local_image_id}'\n")
    (bindir / "docker").chmod(0o755)
    snippet = f"""
set -uo pipefail
REPO_ROOT="{repo}"; VERSION="{VERSION}"; RED=''; NC=''
SCRIPT_DIR="{RELEASE_DIR}"; CRITERIA_PY="{sys.executable}"
cd "{repo}" || exit 2
record() {{ printf '%s\\t%s\\t%s\\n' "$1" "$2" "${{3:-}}" >> "{repo.parent}/records.log"; }}
fail_out() {{ echo "FAIL_OUT=$1"; exit "$1"; }}
{_block()}
echo FELL_THROUGH
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _test_stage_current(repo: Path) -> None:
    """The test stage re-ran on the new HEAD (it must: backend/tests/** is not neutral for it)."""
    head = _git(repo, "rev-parse", "--short", "HEAD")
    (repo / ".release" / VERSION / "steps" / "test").write_text(f"status=done\nsha={head}\n")


def test_a_test_only_fix_reuses_build_scan_and_rehearse_and_records_it(repo: Path) -> None:
    _commit(repo, "backend/tests/unit/test_x.py")
    _test_stage_current(repo)

    rc, out = _run(repo)

    assert rc == 0 and "FELL_THROUGH" in out, out
    assert out.count("REUSED") == 3, out
    for stage in ("build", "scan", "rehearse"):
        text = (repo / ".release" / VERSION / "steps" / f"{stage}.reuse").read_text()
        assert "legs_sha256=" in text and "release_tests_tree=" in text, text


def test_the_test_stage_itself_is_never_reused(repo: Path) -> None:
    _commit(repo, "backend/tests/unit/test_x.py")

    rc, out = _run(repo)

    assert rc == 1 and "test (ran at" in out and "build (ran at" not in out, out


def test_a_rebuilt_image_refuses_reuse(repo: Path) -> None:
    _commit(repo, "backend/tests/unit/test_x.py")
    _test_stage_current(repo)

    rc, out = _run(repo, local_image_id="sha256:bbbb")

    assert rc == 1 and "is not the recorded build" in out, out
    assert not list((repo / ".release" / VERSION / "steps").glob("*.reuse"))


def test_a_changed_rehearsal_script_refuses_reuse(repo: Path) -> None:
    _commit(repo, "backend/tests/unit/test_x.py", "scripts/release-tests/t.sh")
    _test_stage_current(repo)

    rc, out = _run(repo)

    assert rc == 1 and "scripts/release-tests/** changed" in out, out


def test_missing_recorded_image_ids_refuse_reuse(repo: Path) -> None:
    (repo / ".release" / VERSION / "legs.tsv").unlink()
    _commit(repo, "backend/tests/unit/test_x.py")
    _test_stage_current(repo)

    rc, out = _run(repo)

    assert rc == 1 and "no recorded image IDs" in out, out


def test_a_shipped_source_change_is_never_reusable_even_with_identical_ids(repo: Path) -> None:
    _commit(repo, "backend/tests/unit/test_x.py", "backend/app/main.py")
    _test_stage_current(repo)

    rc, out = _run(repo)

    assert rc == 1 and "neither neutral nor image-free" in out, out


def test_every_reuse_glob_is_excluded_from_its_image_by_dockerignore() -> None:
    """A future reuse glob cannot cover a path that ships: it must be in the component's
    .dockerignore (backend/tests/** <-> backend/.dockerignore `tests/`)."""
    policy = yaml.safe_load(CRITERIA.read_text(encoding="utf-8"))["evidence_reuse_paths"]
    assert policy, "the reuse policy vanished: the L1 tests would pass on nothing"
    for stage, globs in policy.items():
        for glob in globs:
            component, _, rest = glob.partition("/")
            assert glob.endswith("/**") and rest, f"{stage}: {glob} must be a component/dir/** glob"
            ignore = (REPO_ROOT / component / ".dockerignore").read_text(encoding="utf-8")
            lines = {ln.strip() for ln in ignore.splitlines()}
            assert f"{rest[:-3]}/" in lines, (
                f"{stage}: {glob} is not excluded by {component}/.dockerignore, so a change there "
                f"CAN change an image"
            )
