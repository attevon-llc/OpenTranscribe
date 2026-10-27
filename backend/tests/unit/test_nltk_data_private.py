"""nltk >= 3.10.4 reads a group/world-writable data tree as an ABSENT corpus.

3.10.4 (the release that fixes GHSA-j456-xh4h-cpf2) judges the data directories
themselves: ``nltk.data.find`` refuses a root, or any file under it, that is writable
by group or world, and ``nltk.download`` refuses to install into one. The refusal is a
``LookupError``, which every caller in this app treats as "corpus missing" and answers
with its degraded mode -- so a bind-mounted ``models/nltk_data`` created under the
Ubuntu-default umask of 002 (mode 775) would quietly turn sentence splitting, stopwords
and topic extraction into their fallbacks, with no failing request to point at it.

``ensure_nltk_data_private`` is the fix, run once at API and worker start. These tests
drive the real function against real directory trees and the real ``nltk.data.find``.
The last one only discriminates under nltk >= 3.10.4; under 3.10.3 it passes both with
and without the helper, which is why the version-independent tests above it exist.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from app.utils import nltk_offline
from app.utils.nltk_offline import ensure_nltk_data_private

BACKEND = Path(__file__).resolve().parents[2]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _tree(root: Path, dir_mode: int, file_mode: int) -> tuple[Path, Path, Path]:
    """root/tokenizers/punkt_tab/english/collocations.tab, every entry at the given modes."""
    leaf_dir = root / "tokenizers" / "punkt_tab" / "english"
    leaf_dir.mkdir(parents=True)
    leaf = leaf_dir / "collocations.tab"
    leaf.write_text("a\tb\n")
    leaf.chmod(file_mode)
    for d in (leaf_dir, leaf_dir.parent, leaf_dir.parent.parent, root):
        d.chmod(dir_mode)
    return root, leaf_dir, leaf


def test_group_writable_tree_is_made_private_and_keeps_owner_bits(tmp_path):
    root, leaf_dir, leaf = _tree(tmp_path / "nltk_data", 0o775, 0o664)

    assert ensure_nltk_data_private([str(root)]) == []

    assert _mode(root) == 0o755
    assert _mode(leaf_dir) == 0o755
    assert _mode(leaf) == 0o644


def test_world_writable_is_stripped_too(tmp_path):
    root, _, leaf = _tree(tmp_path / "nltk_data", 0o777, 0o666)

    ensure_nltk_data_private([str(root)])

    assert _mode(root) == 0o755
    assert _mode(leaf) == 0o644


def test_an_already_private_tree_is_left_exactly_as_it_was(tmp_path):
    root, leaf_dir, leaf = _tree(tmp_path / "nltk_data", 0o700, 0o600)

    assert ensure_nltk_data_private([str(root)]) == []

    assert (_mode(root), _mode(leaf_dir), _mode(leaf)) == (0o700, 0o700, 0o600)


def test_a_symlink_is_not_followed_to_chmod_its_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o777)
    root = tmp_path / "nltk_data"
    root.mkdir()
    (root / "link").symlink_to(outside)

    ensure_nltk_data_private([str(root)])

    assert _mode(outside) == 0o777, "chmod followed a symlink out of the data root"


def test_defaults_to_the_nltk_data_environment_variable(tmp_path, monkeypatch):
    root, _, leaf = _tree(tmp_path / "nltk_data", 0o775, 0o664)
    monkeypatch.setenv("NLTK_DATA", f"{tmp_path / 'absent'}{os.pathsep}{root}")

    assert ensure_nltk_data_private() == []

    assert _mode(leaf) == 0o644


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root owns everything, so nothing is 'another account'"
)
def test_an_entry_owned_by_another_account_is_reported_and_untouched(tmp_path, monkeypatch, caplog):
    root, _, leaf = _tree(tmp_path / "nltk_data", 0o775, 0o664)
    # The tree is owned by us; make the process look like a different, non-root user.
    monkeypatch.setattr(nltk_offline.os, "geteuid", lambda: os.getuid() + 1)

    with caplog.at_level(logging.WARNING, logger=nltk_offline.logger.name):
        unfixable = ensure_nltk_data_private([str(root)])

    assert str(leaf) in unfixable
    assert _mode(leaf) == 0o664, "chmod'd a file the process does not own"
    assert any("fix-model-permissions.sh" in r.getMessage() for r in caplog.records)


def test_a_read_only_mount_is_reported_not_raised(tmp_path, monkeypatch, caplog):
    root, _, leaf = _tree(tmp_path / "nltk_data", 0o775, 0o664)

    def _erofs(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(nltk_offline.os, "chmod", _erofs)

    with caplog.at_level(logging.WARNING, logger=nltk_offline.logger.name):
        unfixable = ensure_nltk_data_private([str(root)])

    assert str(leaf) in unfixable
    assert any("read-only" in r.getMessage() for r in caplog.records)


def test_a_missing_root_is_not_an_error(tmp_path):
    assert ensure_nltk_data_private([str(tmp_path / "does-not-exist")]) == []


def test_nltk_can_read_the_tree_after_the_helper_runs(tmp_path):
    """The end-to-end contract, against the real ``nltk.data.find``.

    Under nltk >= 3.10.4 this raises ``LookupError`` ("Refused, writable by other
    accounts") without the helper; under 3.10.3 it passes either way.
    """
    import nltk.data

    root, _, _ = _tree(tmp_path / "nltk_data", 0o775, 0o664)

    ensure_nltk_data_private([str(root)])

    found = nltk.data.find("tokenizers/punkt_tab/english/collocations.tab", paths=[str(root)])
    assert str(found).endswith("collocations.tab")


@pytest.mark.parametrize(
    ("relative", "needle"),
    [
        ("app/core/celery.py", "ensure_nltk_data_private()"),
        ("app/main.py", "ensure_nltk_data_private()"),
        ("../scripts/download-models.py", "ensure_nltk_data_private("),
    ],
)
def test_every_process_that_touches_the_cache_calls_the_helper(relative, needle):
    """Wiring guard: the helper is worthless if the entry points never call it."""
    source = (BACKEND / relative).read_text()

    calls = [
        line
        for line in source.splitlines()
        if line.strip().startswith(needle) and "import" not in line
    ]
    assert len(calls) == 1, (
        f"{relative} should call ensure_nltk_data_private exactly once, found {calls}"
    )
