"""opentr.sh must not let a git worktree silently run with an empty ``.env``.

``.env`` is gitignored, so it exists only in the MAIN checkout, never in a
``.claude/worktrees/<name>`` (or any other) worktree (issue #961a). Left alone,
every ``*_PASSWORD``/``*_PORT`` var compose interpolates comes out empty and the
first symptom is Postgres crash-looping on "superuser password is not
specified" -- nothing in that output says ".env is missing", so a reader
reasonably (and wrongly) suspects a corrupt volume.

Two behaviors are pinned here, both extracted from the REAL script source
(never reimplemented) and driven via subprocess bash, so a regression in the
actual script fails here:

1. The top-level auto-link prologue: when ``.env`` is absent and this is a git
   worktree whose MAIN checkout has one, opentr.sh creates a RELATIVE symlink
   to it -- never a copy, and the file's contents are never opened by the
   script (only existence is checked).
2. ``require_env_file_or_die``: when no ``.env`` can be found anywhere (not
   even in the main checkout), a real start refuses with a diagnosis naming
   the main checkout, instead of silently proceeding with every var empty.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included."""
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _autolink_prologue(text: str) -> str:
    """Source of the top-level ``.env`` auto-link block (not inside a function)."""
    start_marker = "\n# .env is gitignored, so a git worktree"
    end_marker = "\n# Load environment variables from .env if present\n"
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    assert start < end, "auto-link prologue markers moved -- update this test"
    return text[start:end]


def _make_worktree(tmp_path: Path, *, main_has_env: bool) -> tuple[Path, Path]:
    """Real ``git worktree add`` fixture -- exercises git's own worktree
    detection (``--git-common-dir`` vs ``--git-dir``), not a simulation of it.
    """
    main = tmp_path / "main"
    main.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731 - local test helper
        args, cwd=main, check=True, capture_output=True, text=True
    )
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (main / "README.md").write_text("x")
    run("git", "add", ".")
    run("git", "commit", "-q", "-m", "init")
    if main_has_env:
        (main / ".env").write_text("FIXTURE_ONLY=1\n")
    wt = tmp_path / "wt"
    run("git", "worktree", "add", "-q", str(wt), "-b", "wtbranch")
    return main, wt


def _run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


def test_autolink_creates_a_relative_symlink_to_the_main_checkouts_env(tmp_path: Path):
    main, wt = _make_worktree(tmp_path, main_has_env=True)
    body = _autolink_prologue(OPENTR.read_text(encoding="utf-8"))
    result = _run_bash(body, cwd=wt)
    assert result.returncode == 0, result.stderr

    env_link = wt / ".env"
    assert env_link.is_symlink(), "expected .env to be created as a symlink"
    target = os.readlink(env_link)
    assert not target.startswith("/"), f"expected a RELATIVE symlink, got {target!r}"
    assert env_link.resolve().samefile(main / ".env")


def test_autolink_does_nothing_when_the_main_checkout_has_no_env_either(tmp_path: Path):
    _main, wt = _make_worktree(tmp_path, main_has_env=False)
    body = _autolink_prologue(OPENTR.read_text(encoding="utf-8"))
    result = _run_bash(body, cwd=wt)
    assert result.returncode == 0, result.stderr
    assert not (wt / ".env").exists()


def test_autolink_does_nothing_in_an_ordinary_non_worktree_checkout(tmp_path: Path):
    solo = tmp_path / "solo"
    solo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=solo, check=True, capture_output=True)
    body = _autolink_prologue(OPENTR.read_text(encoding="utf-8"))
    result = _run_bash(body, cwd=solo)
    assert result.returncode == 0, result.stderr
    assert not (solo / ".env").exists()


def test_require_env_file_or_die_exits_nonzero_and_names_the_main_checkout(tmp_path: Path):
    main, wt = _make_worktree(tmp_path, main_has_env=False)
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "require_env_file_or_die")
    script = f"{body}\nrequire_env_file_or_die\n"
    result = _run_bash(script, cwd=wt)
    assert result.returncode == 1
    assert "worktree" in result.stdout
    assert str(main.resolve()) in result.stdout


def test_require_env_file_or_die_is_a_noop_when_env_exists(tmp_path: Path):
    (tmp_path / ".env").write_text("X=1\n")
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "require_env_file_or_die")
    script = f"{body}\nrequire_env_file_or_die && echo SURVIVED\n"
    result = _run_bash(script, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "SURVIVED" in result.stdout
