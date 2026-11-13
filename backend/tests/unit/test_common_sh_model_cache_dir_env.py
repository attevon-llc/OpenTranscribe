"""scripts/common.sh's model-cache helpers must honour the exported MODEL_CACHE_DIR.

opentr.sh resolves MODEL_CACHE_DIR worktree-aware (the MAIN checkout's cache) and exports it, so
compose bind-mounts that. The helpers used to start with `local MODEL_CACHE_DIR=""` and re-read the
raw .env value, defaulting to ./models, which from a git worktree is a directory no container
mounts: NLTK and OpenSearch models were re-downloaded into it on every worktree `--fresh` start.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON = REPO_ROOT / "scripts" / "common.sh"

_DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
exit 1
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    main.mkdir()
    (main / "models").mkdir()
    (main / "README").write_text("x")
    _git(tmp_path, "init", "-q", str(main))
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "base")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt))
    return main, wt


def _populate(main: Path, kind: str) -> None:
    models = main / "models"
    if kind == "nltk":
        (models / "nltk_data" / "tokenizers").mkdir(parents=True)
        (models / "nltk_data" / "tokenizers" / "punkt").write_text("x")
    elif kind == "opensearch":
        (models / "opensearch-ml" / "all-MiniLM-L6-v2").mkdir(parents=True)
        (models / "opensearch-ml" / "all-MiniLM-L6-v2" / "m").write_text("x")
    elif kind == "linked":
        (models / "nltk_data").mkdir(parents=True)
        (models / "nltk_data" / "a").write_text("x")
        os.link(models / "nltk_data" / "a", models / "nltk_data" / "b")


# (function, what the MAIN cache holds, text proving the helper looked at the MAIN cache)
CASES = [
    ("fix_model_cache_permissions", "none", None),
    ("ensure_nltk_corpora", "nltk", None),
    ("ensure_opensearch_models", "opensearch", "neural models found"),
    ("ensure_nltk_data_unlinked", "linked", "De-hardlinking"),
]


@pytest.mark.parametrize(("function", "kind", "expected"), CASES)
def test_helpers_work_on_the_exported_cache_never_the_worktrees(
    worktree: tuple[Path, Path], tmp_path: Path, function: str, kind: str, expected: str | None
) -> None:
    main, wt = worktree
    _populate(main, kind)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(_DOCKER_STUB)
    (bindir / "docker").chmod(0o755)
    env = {
        **{k: v for k, v in os.environ.items() if k != "MODEL_CACHE_DIR"},
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "docker.log"),
        "MODEL_CACHE_DIR": str(main / "models"),
    }
    proc = subprocess.run(
        ["bash", "-c", f'source "{COMMON}"; {function} 2>&1; echo "RC=$?"'],
        cwd=wt,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "RC=" in proc.stdout, proc.stderr
    assert not (wt / "models").exists(), (
        f"{function} created or used <worktree>/models, a directory no container mounts"
    )
    if expected:
        assert expected in proc.stdout, proc.stdout
    if kind == "nltk":
        assert "not found" not in proc.stdout, "ensure_nltk_corpora probed <worktree>/models"
