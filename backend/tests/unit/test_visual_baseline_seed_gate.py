"""The seed directory must resolve from a git worktree (#973).

Run from a git worktree, a capture seeded **zero completed files** and photographed the result
anyway. ``scripts/seed-fresh-deployment.sh`` defaulted ``SEED_MEDIA_DIR`` to the RELATIVE path
``benchmark/test_audio``. ``benchmark/`` is gitignored, so it exists only in the main checkout;
from ``.claude/worktrees/<name>`` the path resolves to nothing, the script falls back to
synthetic silence, and the pipeline (correctly) marks every file ``error``.

Tested against the real script text, not a re-implementation: only the resolver function is
sourced, inside a genuine ``git worktree``. The other half of #973 -- the capture wrapper's
corpus gate -- lives in ``test_visual_baseline_corpus_gate.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed-fresh-deployment.sh"


def _git(cwd: Path, *args: str) -> str:
    """Run git in a sandbox that ignores the developer's own global/system config."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _resolver_function() -> str:
    """The seed script's default-directory resolver, extracted verbatim."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^_default_seed_media_dir\(\) \{\n.*?^\}\n", text, re.DOTALL | re.MULTILINE)
    assert match is not None, "seed-fresh-deployment.sh has no _default_seed_media_dir()"
    return match.group(0)


def _resolve_from(cwd: Path) -> str:
    return subprocess.run(
        ["bash", "-c", f"set -u\n{_resolver_function()}\n_default_seed_media_dir"],
        cwd=cwd,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo_with_corpus(tmp_path: Path) -> Path:
    """A main checkout holding a gitignored ``benchmark/test_audio`` corpus."""
    main = tmp_path / "main-checkout"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README").write_text("x\n", encoding="utf-8")
    (main / ".gitignore").write_text("benchmark/\n", encoding="utf-8")
    _git(main, "add", "README", ".gitignore")
    _git(main, "commit", "-q", "-m", "init")
    corpus = main / "benchmark" / "test_audio"
    corpus.mkdir(parents=True)
    (corpus / "real.wav").write_bytes(b"RIFF")
    return main


class TestTheSeedDirectoryResolvesFromAWorktree:
    def test_a_worktree_finds_the_main_checkouts_gitignored_corpus(
        self, repo_with_corpus: Path, tmp_path: Path
    ):
        worktree = tmp_path / "wt"
        _git(repo_with_corpus, "worktree", "add", "-q", "--detach", str(worktree))
        assert not (worktree / "benchmark").exists(), "precondition: gitignored, so absent here"

        resolved = _resolve_from(worktree)

        assert resolved == str(repo_with_corpus / "benchmark" / "test_audio")
        assert Path(resolved).is_dir()

    def test_the_main_checkout_resolves_to_its_own_corpus_as_an_absolute_path(
        self, repo_with_corpus: Path
    ):
        resolved = _resolve_from(repo_with_corpus)

        assert resolved == str(repo_with_corpus / "benchmark" / "test_audio")

    def test_with_no_corpus_anywhere_the_relative_default_is_kept(self, tmp_path: Path):
        bare = tmp_path / "bare"
        bare.mkdir()
        _git(bare, "init", "-q")

        assert _resolve_from(bare) == "benchmark/test_audio"

    def test_outside_a_git_repository_the_relative_default_is_kept(self, tmp_path: Path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()

        assert _resolve_from(plain) == "benchmark/test_audio"

    def test_an_explicit_seed_media_dir_still_wins(self):
        text = SEED_SCRIPT.read_text(encoding="utf-8")

        assert 'SEED_MEDIA_DIR="${SEED_MEDIA_DIR:-$(_default_seed_media_dir)}"' in text
