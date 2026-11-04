"""Path-aware evidence freshness in `70-tag.sh` (`artifacts-match-tagged-commit`).

A docs-only commit made after the evidence (CHANGELOG, blog, docs pages) used to make
test/rehearse evidence look stale although docs cannot change tested code. The policy is the
`evidence_neutral_paths` table in release-criteria.yaml; build/scan deliberately exclude
`docs-site/**` because the docs image bakes it in.

The REAL block is extracted from 70-tag.sh and run in a throwaway repo with a fake ledger,
against the REAL policy file and helper (so editing the policy fails these tests).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"
TAG_SH = RELEASE_DIR / "70-tag.sh"
TOOL = RELEASE_DIR / "evidence_neutral.py"
CRITERIA = RELEASE_DIR / "release-criteria.yaml"
GIT = shutil.which("git") or "git"
VERSION = "v9.9.9"
STAGES = ("test", "build", "scan", "rehearse")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_spec = importlib.util.spec_from_file_location("evidence_neutral", TOOL)
assert _spec is not None and _spec.loader is not None
evidence_neutral = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evidence_neutral)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _block() -> str:
    text = TAG_SH.read_text(encoding="utf-8")
    start = text.index("# --- BEGIN artifacts-match-tagged-commit ---")
    end = text.index("# --- END artifacts-match-tagged-commit ---", start)
    return text[start : text.index("\n", end) + 1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("1")
    (r / ".gitignore").write_text(".release/\n")  # as in the real repo: the ledger is untracked
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _commit(repo: Path, *rel_paths: str) -> None:
    for rel in rel_paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(target.read_text() + "x" if target.exists() else "x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delta")


def _ledger(repo: Path, stage: str, sha: str) -> None:
    steps = repo / ".release" / VERSION / "steps"
    steps.mkdir(parents=True, exist_ok=True)
    (steps / stage).write_text(f"status=done\nsha={sha}\n", encoding="utf-8")


def _run(repo: Path) -> tuple[int, str, str]:
    """Returns (rc, combined output, the detail the block passed to `record ... fail`)."""
    log = repo.parent / "records.log"
    log.unlink(missing_ok=True)
    snippet = f"""
set -uo pipefail
REPO_ROOT="{repo}"; VERSION="{VERSION}"; RED=''; NC=''
SCRIPT_DIR="{RELEASE_DIR}"
CRITERIA_PY="{sys.executable}"
cd "{repo}" || exit 2
record() {{ printf '%s\\t%s\\t%s\\n' "$1" "$2" "${{3:-}}" >> "{log}"; }}
fail_out() {{ echo "FAIL_OUT=$1"; exit "$1"; }}
{_block()}
echo FELL_THROUGH
"""
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    detail = ""
    if log.exists():
        parts = log.read_text(encoding="utf-8").splitlines()[0].split("\t")
        detail = parts[2] if parts[1] == "fail" else ""
    return proc.returncode, proc.stdout + proc.stderr, detail


def _all_at(repo: Path, sha: str) -> None:
    for s in STAGES:
        _ledger(repo, s, sha)


@pytest.mark.unit
def test_equal_sha_is_fresh_for_every_stage(repo: Path) -> None:
    _all_at(repo, _git(repo, "rev-parse", "--short", "HEAD"))

    rc, out, _ = _run(repo)

    assert rc == 0 and "FELL_THROUGH" in out, out
    assert out.count("ran on HEAD") == 4, out


@pytest.mark.unit
def test_docs_only_delta_is_fresh_for_every_stage(repo: Path) -> None:
    base = _git(repo, "rev-parse", "--short", "HEAD")
    _all_at(repo, base)
    _commit(repo, "CHANGELOG.md", "docs/blog/post.md", "README.md")

    rc, out, _ = _run(repo)

    assert rc == 0, out
    assert out.count("only evidence-neutral paths changed since " + base) == 4, out


@pytest.mark.unit
def test_docs_site_delta_is_fresh_for_test_and_rehearse_but_stale_for_build_and_scan(
    repo: Path,
) -> None:
    _all_at(repo, _git(repo, "rev-parse", "--short", "HEAD"))
    _commit(repo, "docs-site/docs/page.md")

    rc, out, detail = _run(repo)

    assert rc == 1, out
    assert "build (" in detail and "scan (" in detail, detail
    assert "test (" not in detail and "rehearse (" not in detail, detail
    assert "docs-site/docs/page.md" in detail, detail
    assert "test: fresh" in out and "rehearse: fresh" in out, out


@pytest.mark.unit
def test_a_code_delta_is_stale_for_every_stage_and_names_the_path(repo: Path) -> None:
    _all_at(repo, _git(repo, "rev-parse", "--short", "HEAD"))
    _commit(repo, "docs/ok.md", "backend/app/x.py")

    rc, out, detail = _run(repo)

    assert rc == 1, out
    for s in STAGES:
        assert f"{s} (" in detail, detail
    assert "backend/app/x.py" in detail and "docs/ok.md" not in detail, detail


@pytest.mark.unit
def test_a_sha_that_is_not_an_ancestor_is_stale(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "docs/side.md")
    side = _git(repo, "rev-parse", "--short", "HEAD")
    _git(repo, "checkout", "-q", "-")
    _commit(repo, "docs/main.md")
    _all_at(repo, side)

    rc, out, detail = _run(repo)

    assert rc == 1 and "not an ancestor" in detail, out


@pytest.mark.unit
def test_an_unresolvable_sha_is_stale(repo: Path) -> None:
    _all_at(repo, "unknown")

    rc, _, detail = _run(repo)

    assert rc == 1 and "does not resolve" in detail, detail


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("docs/**", "docs/a/b.md", True),
        ("docs/**", "docs-site/a.md", False),
        ("docs/**", "docs", False),
        ("*.md", "README.md", True),
        ("*.md", "backend/README.md", False),
        ("CHANGELOG.md", "CHANGELOG.md", True),
        ("CHANGELOG.md", "x/CHANGELOG.md", False),
        (".release/**", ".release/v1/steps/test", True),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert evidence_neutral.matches(pattern, path) is expected


@pytest.mark.unit
def test_policy_declares_docs_site_neutral_only_where_the_image_does_not_bake_it() -> None:
    policy = yaml.safe_load(CRITERIA.read_text(encoding="utf-8"))["evidence_neutral_paths"]

    assert set(policy) == set(STAGES)
    for stage in ("test", "rehearse"):
        assert "docs-site/**" in policy[stage] and ".release/**" in policy[stage]
    for stage in ("build", "scan"):
        assert "docs-site/**" not in policy[stage] and ".release/**" not in policy[stage]
