"""`70-tag.sh` must cut (or confirm) `release/<major>.<minor>` FROM THE TAG it
just pushed, and must fail when a tag was cut from somewhere else (issue #784).

Also covers finding N4: `git tag -a` and `git push origin "$VERSION"` at the
tail of `70-tag.sh` used to be UNCHECKED (the file is `set -uo pipefail`, no
`-e`), so a failed push fell through to a recorded PASS and `exit 0`.

This drives the REAL `70-tag.sh` against a REAL bare git repo standing in for
`origin` (never a re-implementation of `git push`/`fetch`/`merge-base
--is-ancestor` semantics), with `check-version-consistency.py` stubbed out
(its own correctness is covered elsewhere) and `criteria-lib.sh` copied in
unmodified.

Three cases:

1. must-fire / first cut — `release/0.5` does not exist yet; `70-tag.sh` must
   create it FROM the tag and the ancestor check must pass.
2. must-stay-clean / patch path — `release/0.5` already exists (as it would
   after a hotfix PR merged into it); `70-tag.sh` must NOT recreate it, only
   confirm the tag is an ancestor.
3. must-fire / cut from somewhere else — a tag whose commit is NOT a
   descendant of `origin/release/<minor>`'s current tip must FAIL the
   `release-branch-tracks-tag` criterion and exit 1. This is the guard #784
   asks for: a patch accidentally cut from `master` instead of the release
   branch must be caught here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"
TAG_SH = RELEASE_DIR / "70-tag.sh"
CRITERIA_LIB_SH = RELEASE_DIR / "criteria-lib.sh"

pytestmark = [
    pytest.mark.skipif(
        not TAG_SH.exists() or not CRITERIA_LIB_SH.exists(),
        reason="scripts/release/{70-tag,criteria-lib}.sh missing",
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

GIT = shutil.which("git") or "git"

_CRITERIA_YAML = """
version: 2
stages:
  tag:
    criteria:
      - id: clean-worktree
        severity: blocking
      - id: tag-absent
        severity: blocking
      - id: version-consistency-pre-tag
        severity: blocking
      - id: changelog-section
        severity: blocking
      - id: tag-pushed
        severity: blocking
      - id: release-branch-tracks-tag
        severity: blocking
"""

_CHECK_VERSION_CONSISTENCY_STUB = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Parameterised on `str` because `text=True` is set: without it CompletedProcess is
    # implicitly CompletedProcess[Any], so every `.stdout` downstream is Any and mypy
    # flags the callers rather than this line. Fixing it here keeps those callers
    # checked, which annotating each of them `-> Any` would have deleted.
    return subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _changelog(*sections: str) -> str:
    body = "\n\n".join(f"## [{s}]\n\nNotes for {s}.\n" for s in sections)
    return f"# Changelog\n\n{body}\n"


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    return bare


@pytest.fixture
def work(tmp_path: Path, origin: Path) -> Path:
    """A working checkout with `scripts/release/{70-tag,criteria-lib}.sh` +
    stubs installed, an `origin` remote, and one commit on `master` (already
    pushed) with a CHANGELOG covering 0.5.0.
    """
    w = tmp_path / "work"
    w.mkdir()
    _git(w, "init", "-q", ".")
    _git(w, "config", "user.email", "t@example.invalid")
    _git(w, "config", "user.name", "t")
    _git(w, "remote", "add", "origin", str(origin))

    release_dir = w / "scripts" / "release"
    release_dir.mkdir(parents=True)
    shutil.copy(TAG_SH, release_dir / "70-tag.sh")
    (release_dir / "70-tag.sh").chmod(0o755)
    shutil.copy(CRITERIA_LIB_SH, release_dir / "criteria-lib.sh")
    (release_dir / "release-criteria.yaml").write_text(_CRITERIA_YAML)
    check_stub = release_dir / "check-version-consistency.py"
    check_stub.write_text(_CHECK_VERSION_CONSISTENCY_STUB)
    check_stub.chmod(0o755)

    (w / "CHANGELOG.md").write_text(_changelog("0.5.0"))
    _git(w, "add", "-A")
    _git(w, "commit", "-q", "-m", "initial")
    _git(w, "push", "-q", "origin", "HEAD:refs/heads/master")
    return w


def _run_tag(work: Path, version: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(work / "scripts" / "release" / "70-tag.sh"), version],
        cwd=work,
        env={"PATH": "/usr/bin:/bin", "JSON_OUT": "false"},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _remote_branch_exists(origin: Path, branch: str) -> bool:
    proc = subprocess.run(
        [GIT, "-C", str(origin), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
    )
    return proc.returncode == 0


def _remote_rev(origin: Path, ref: str) -> str:
    return _git(origin, "rev-parse", ref).stdout.strip()


def _remote_tag_exists(origin: Path, tag: str) -> bool:
    proc = subprocess.run(
        [GIT, "-C", str(origin), "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
        capture_output=True,
    )
    return proc.returncode == 0


@pytest.mark.unit
def test_first_cut_creates_the_release_branch_from_the_tag(work: Path, origin: Path) -> None:
    proc = _run_tag(work, "v0.5.0")

    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert _remote_tag_exists(origin, "v0.5.0")
    assert _remote_branch_exists(origin, "release/0.5"), "release/0.5 was not cut"
    # `^{}` peels the annotated tag object down to the commit it names -- the
    # tag ref itself resolves to a TAG object (this pipeline always tags with
    # `git tag -a`), which a branch can never point at directly.
    assert _remote_rev(origin, "refs/heads/release/0.5") == _remote_rev(
        origin, "refs/tags/v0.5.0^{}"
    ), "release/0.5 must point at exactly the commit the tag names on the first cut"
    assert "tag-pushed" in proc.stderr
    assert "release-branch-tracks-tag" in proc.stderr


@pytest.mark.unit
def test_patch_path_confirms_without_recreating_the_branch(work: Path, origin: Path) -> None:
    """Must-stay-clean: once release/0.5 already exists (as it would after a
    hotfix PR merged into it), 70-tag.sh must not recreate it — only confirm
    the new tag is an ancestor of its current tip.
    """
    first = _run_tag(work, "v0.5.0")
    assert first.returncode == 0, first.stderr

    # Simulate "the hotfix PR already merged into release/0.5": commit forward
    # and push that same commit as the new tip of release/0.5 BEFORE tagging.
    (work / "CHANGELOG.md").write_text(_changelog("0.5.0", "0.5.1"))
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "hotfix")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/release/0.5")

    proc = _run_tag(work, "v0.5.1")

    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "already exists on origin" in proc.stderr, proc.stderr
    assert _remote_rev(origin, "refs/heads/release/0.5") == _remote_rev(
        origin, "refs/tags/v0.5.1^{}"
    )


@pytest.mark.unit
def test_a_tag_cut_from_somewhere_else_fails_the_ancestry_check(work: Path, origin: Path) -> None:
    """Must-fire: the actual guard #784 exists for. `release/0.9` already
    points at a DIVERGENT history; a v0.9.5 tag cut from a sibling commit
    (think: accidentally tagged from master) must fail here rather than
    silently rewriting what release/0.9 means.
    """
    # release/0.9 = a commit reachable from work's current HEAD (C0) but
    # extended down its OWN, divergent path (C0 -> C1).
    _git(work, "checkout", "-q", "-b", "diverge-branch")
    (work / "release-only.txt").write_text("release/0.9 content")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "release/0.9 own history")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/release/0.9")

    # Back on the original line (C0), commit a DIFFERENT, sibling change (C2)
    # -- not reachable from C1 -- and tag v0.9.5 there.
    _git(work, "checkout", "-q", "master")
    (work / "CHANGELOG.md").write_text(_changelog("0.5.0", "0.9.5"))
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "unrelated change on master")

    proc = _run_tag(work, "v0.9.5")

    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "release-branch-tracks-tag" in proc.stderr
    assert "cut from somewhere else" in proc.stderr, proc.stderr
    # The tag itself was still pushed (tag-pushed is a separate, earlier
    # criterion) -- what must NOT have happened is release/0.9 changing.
    assert _remote_tag_exists(origin, "v0.9.5")
    assert _remote_rev(origin, "refs/heads/release/0.9") != _remote_rev(
        origin, "refs/tags/v0.9.5^{}"
    )
