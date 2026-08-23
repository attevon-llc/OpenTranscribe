"""The release-test model cache must never hand nltk a multiply-linked file.

nltk 3.10 added ``pathsec`` hardening against CWE-59 and refuses to open any
file under ``nltk_data`` whose ``st_nlink`` exceeds one. ``requirements.txt``
pins ``nltk==3.10.3`` on purpose ("pathsec hardening is required, not
avoided"), so this is a property the application depends on.

Both rehearsal scenarios seeded their per-run model cache with
``rsync -a --link-dest=<src> <src> <dst>``, which hardlinks every file. That is
correct and cheap for the HuggingFace/torch/sentence-transformers trees and
fatal for ``nltk_data``: it raised every file to ``st_nlink >= 2`` on the first
run, and **every transcription in both scenarios then failed** with

    Security Violation [pathsec.open]: refusing multiply-linked file
    '…/punkt_tab/english/collocations.tab' (st_nlink=3)

The failure surfaced only as ``status=error`` on the uploaded file, with the
real message in a database column the harness dropped on teardown, so it read
as a product bug rather than a harness bug.

These tests exercise ``lib/model-cache.sh`` for real — building hardlinked
trees on disk and running the shell functions against them — rather than
grepping the scripts for a string. A grep would pass against a helper that
does nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_DIR = REPO_ROOT / "scripts" / "release-tests" / "lib"
MODEL_CACHE_LIB = LIB_DIR / "model-cache.sh"

#: Stub the guardrails logging helpers so the lib can be exercised in
#: isolation. ``gr_die`` must exit non-zero — that is the behaviour under test.
_GR_STUBS = (
    "gr_log(){ :; }; gr_ok(){ :; }; "
    'gr_warn(){ echo "WARN: $*" >&2; }; '
    'gr_die(){ echo "DIE: $*" >&2; exit 1; }; '
)


def _run_lib(snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet with model-cache.sh sourced and guardrails stubbed."""
    script = f"set -uo pipefail\n{_GR_STUBS}\nsource {MODEL_CACHE_LIB}\n{snippet}\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _make_hardlinked_tree(root: Path, n_files: int = 3) -> Path:
    """A source tree plus a hardlinked copy — the shape the harness produced."""
    src = root / "src" / "nltk_data" / "tokenizers"
    src.mkdir(parents=True)
    for i in range(n_files):
        (src / f"f{i}.tab").write_text(f"payload {i}\n")

    dst = root / "dst" / "nltk_data" / "tokenizers"
    dst.mkdir(parents=True)
    for i in range(n_files):
        os.link(src / f"f{i}.tab", dst / f"f{i}.tab")
    return root / "dst" / "nltk_data"


def _link_counts(tree: Path) -> list[int]:
    return sorted(p.stat().st_nlink for p in tree.rglob("*") if p.is_file())


def test_the_lib_exists_and_names_nltk_data_as_pathsec_sensitive():
    """The invariant is declared in one place, and it covers nltk_data."""
    assert MODEL_CACHE_LIB.is_file(), f"{MODEL_CACHE_LIB} is missing"
    result = _run_lib('printf "%s\\n" "${MC_PATHSEC_SUBDIRS[@]}"')
    assert result.returncode == 0, f"sourcing model-cache.sh failed: {result.stderr}"
    assert "nltk_data" in result.stdout.split(), (
        f"nltk_data is not listed as pathsec-sensitive; got: {result.stdout!r}"
    )


def test_the_fixture_really_produces_multiply_linked_files(tmp_path):
    """Guard the guard: if this is not red, every other test here is vacuous."""
    tree = _make_hardlinked_tree(tmp_path)
    counts = _link_counts(tree)
    assert counts, "fixture produced no files"
    assert all(c > 1 for c in counts), (
        f"fixture failed to create hardlinks; link counts were {counts}"
    )


def test_break_hardlinks_makes_every_file_independent(tmp_path):
    """The repair path: st_nlink drops to 1 and content is preserved."""
    tree = _make_hardlinked_tree(tmp_path)
    before = _link_counts(tree)

    result = _run_lib(f'mc_break_hardlinks "{tree}"')
    assert result.returncode == 0, f"mc_break_hardlinks failed: {result.stderr}"

    after = _link_counts(tree)
    assert all(c > 1 for c in before), f"precondition lost: {before}"
    assert after == [1] * len(before), f"still multiply linked: {after}"
    assert (tree / "tokenizers" / "f0.tab").read_text() == "payload 0\n", (
        "breaking the hardlink corrupted the file contents"
    )


def test_assert_no_hardlinks_fails_loudly_on_a_poisoned_tree(tmp_path):
    """The gate must reject the exact state that broke the rehearsal."""
    tree = _make_hardlinked_tree(tmp_path)
    result = _run_lib(f'mc_assert_no_hardlinks "{tree}" "unit test"')

    assert result.returncode != 0, (
        "mc_assert_no_hardlinks accepted a multiply-linked tree — the gate is dead"
    )
    combined = result.stdout + result.stderr
    assert "pathsec" in combined.lower(), (
        f"failure message does not name the cause; got: {combined!r}"
    )


def test_assert_no_hardlinks_passes_once_the_tree_is_repaired(tmp_path):
    """The control: the same gate accepts the same tree after repair."""
    tree = _make_hardlinked_tree(tmp_path)
    poisoned = _run_lib(f'mc_assert_no_hardlinks "{tree}" "unit test"')
    assert poisoned.returncode != 0, "control invalid — tree was not poisoned"

    repaired = _run_lib(
        f'mc_break_hardlinks "{tree}" && mc_assert_no_hardlinks "{tree}" "unit test"'
    )
    assert repaired.returncode == 0, (
        f"gate still fails after repair: {repaired.stdout + repaired.stderr}"
    )


def test_seeding_nltk_data_never_hardlinks_but_big_trees_still_do(tmp_path):
    """The whole point: cheap hardlinks everywhere except the pathsec tree.

    Seeding must not silently become a full copy of the multi-GB caches — that
    would fix the bug by making every rehearsal far slower, which is a
    regression of its own.
    """
    src = tmp_path / "shared"
    (src / "nltk_data" / "tokenizers").mkdir(parents=True)
    (src / "nltk_data" / "tokenizers" / "punkt.tab").write_text("tok\n")
    (src / "huggingface" / "hub").mkdir(parents=True)
    (src / "huggingface" / "hub" / "model.bin").write_text("weights\n")

    dst = tmp_path / "run"
    dst.mkdir()

    result = _run_lib(f'mc_seed_cache "{src}" "{dst}" nltk_data huggingface')
    assert result.returncode == 0, f"mc_seed_cache failed: {result.stderr}"

    nltk_file = dst / "nltk_data" / "tokenizers" / "punkt.tab"
    hf_file = dst / "huggingface" / "hub" / "model.bin"
    assert nltk_file.is_file(), "nltk_data was not seeded"
    assert hf_file.is_file(), "huggingface was not seeded"

    assert nltk_file.stat().st_nlink == 1, (
        f"nltk_data was hardlinked (st_nlink={nltk_file.stat().st_nlink}) — this is exactly the bug"
    )
    assert nltk_file.read_text() == "tok\n", "seeded nltk_data content differs"
    assert hf_file.stat().st_nlink > 1, (
        "huggingface was copied rather than hardlinked — seeding the real "
        "multi-GB caches by copy would slow every rehearsal"
    )


@pytest.mark.parametrize("script", ["test-fresh-install.sh", "test-upgrade.sh"])
def test_neither_scenario_hardlinks_the_pathsec_tree_directly(script):
    """Both scenarios must seed through the helper, not with a raw --link-dest.

    A direct ``rsync --link-dest`` naming nltk_data reintroduces the bug while
    the helper sits unused beside it, so the gate above would never fire.
    """
    path = REPO_ROOT / "scripts" / "release-tests" / script
    assert path.is_file(), f"{path} is missing"
    text = path.read_text()

    offenders = [
        line.strip() for line in text.splitlines() if "--link-dest" in line and "nltk" in line
    ]
    assert not offenders, f"{script} hardlinks nltk_data directly: {offenders}"
    assert "mc_seed_cache" in text, (
        f"{script} does not seed through mc_seed_cache, so the pathsec gate never runs"
    )
