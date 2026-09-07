"""The NLTK cache must contain no multiply-linked files (issue #491).

NLTK >= 3.10 ships ``nltk/pathsec.py``, which refuses to open any file whose link count
is greater than one::

    PermissionError: Security Violation [pathsec.open]: refusing multiply-linked file
    '.../nltk_data/tokenizers/punkt_tab/english/collocations.tab' (st_nlink=3);
    a hardlink can point at an outside-root inode (CWE-59)

A model cache restored from a backup, or copied with ``cp -al`` / ``rsync --link-dest``,
arrives fully hardlinked. Every punkt read then raises, and because
``clean_segments`` does not catch it, **transcription fails for every file on the box** —
which is exactly what was found running in the dev stack: all 130 files in
``models/nltk_data`` had ``st_nlink == 3`` and ``split_sentences_nltk`` raised in the
backend and both transcription workers, while the host venv (an older NLTK) was fine.

The control is legitimate, so the data is what gets fixed:
``ensure_nltk_data_unlinked`` in ``scripts/common.sh`` runs at every startup. These
tests drive that shell function for real rather than grepping for it — a hardlink is
a filesystem property, and only the filesystem can confirm it was broken.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_SH = REPO_ROOT / "scripts" / "common.sh"
OPENTR_SH = REPO_ROOT / "opentr.sh"

_PUNKT_REL = Path("models/nltk_data/tokenizers/punkt_tab/english/collocations.tab")


def _run_helper(cwd: Path) -> subprocess.CompletedProcess:
    """Source common.sh and run the helper with ``cwd`` as the working directory."""
    return subprocess.run(
        ["bash", "-c", f'set -e; source "{COMMON_SH}"; ensure_nltk_data_unlinked'],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    """A throwaway model cache holding one punkt-shaped file. No .env -> ./models."""
    target = tmp_path / _PUNKT_REL
    target.parent.mkdir(parents=True)
    target.write_text("gutenberg,the\nmr,smith\n")
    return tmp_path


def test_it_breaks_hardlinks_and_preserves_content_exactly(cache: Path):
    """The whole point: link count drops to 1, bytes are untouched."""
    original = cache / _PUNKT_REL
    content_before = original.read_bytes()
    mode_before = original.stat().st_mode

    # Two extra links, matching the st_nlink=3 observed in the live cache.
    for name in ("link_a", "link_b"):
        os.link(original, cache / name)
    assert original.stat().st_nlink == 3

    result = _run_helper(cache)

    assert result.returncode == 0, result.stderr
    assert original.stat().st_nlink == 1, "the hardlink was not broken"
    assert original.read_bytes() == content_before, "content changed — this must be inode-only"
    assert original.stat().st_mode == mode_before, "permissions were not preserved"
    # The other links keep the old inode and the old content; nothing is destroyed.
    assert (cache / "link_a").read_bytes() == content_before


def test_it_is_a_noop_when_nothing_is_hardlinked(cache: Path):
    """Must-stay-clean control: a healthy cache is left alone.

    Without this, a helper that rewrote every file unconditionally — or one that
    silently did nothing at all — would still pass the test above.
    """
    original = cache / _PUNKT_REL
    inode_before = original.stat().st_ino

    result = _run_helper(cache)

    assert result.returncode == 0, result.stderr
    assert original.stat().st_ino == inode_before, "a healthy cache must not be rewritten"
    assert "De-hardlinking" not in result.stdout


def test_it_is_a_noop_when_the_cache_does_not_exist(tmp_path: Path):
    """First boot, before any model has been downloaded."""
    result = _run_helper(tmp_path)

    assert result.returncode == 0, result.stderr


def test_startup_runs_the_helper_on_every_path():
    """Wiring guard: the fix is worthless if `./opentr.sh start` does not call it.

    Both startup paths prepare the model cache (`fix_model_cache_permissions`); each
    must de-hardlink too, or a cache restored from backup breaks transcription again.
    """
    opentr = OPENTR_SH.read_text()

    assert "ensure_nltk_data_unlinked" in COMMON_SH.read_text(), "helper is not defined"

    # Count CALLS, not mentions. A bare `opentr.count(name)` also counts every comment that
    # names the helper, and prose explaining why a nearby block mirrors
    # fix_model_cache_permissions is exactly the sort of comment this file's own conventions
    # encourage — it inflated prep_calls to 4 against 2 real calls and failed a balanced
    # invariant. Same rule test_shell_expansion_guards applies: a name in a comment is not
    # code. A call is the helper at the start of a statement.
    def _calls(name: str) -> int:
        return len(
            [
                line
                for line in opentr.splitlines()
                if line.lstrip().startswith(name) and not line.lstrip().startswith("#")
            ]
        )

    calls = _calls("ensure_nltk_data_unlinked")
    prep_calls = _calls("fix_model_cache_permissions")
    assert prep_calls >= 2, (
        f"expected at least the two startup prep paths, found {prep_calls} — the counter "
        "may have stopped matching the real call shape"
    )

    assert calls == prep_calls, (
        f"opentr.sh calls fix_model_cache_permissions {prep_calls}x but "
        f"ensure_nltk_data_unlinked {calls}x — every model-cache prep path must do both"
    )
