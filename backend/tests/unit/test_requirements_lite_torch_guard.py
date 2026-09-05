"""Standing guard against "the gist said drop torch too" (issue #660 §0.1).

The #660 gist proposed dropping `pyannote.audio` AND `torch`/`torchaudio` from
`requirements-lite.txt`. Only `pyannote.audio` and `torchaudio` can actually go:
`sentence-transformers==6.0.0` (used at `opensearch_service/client.py` and
`chat/reranker.py` for semantic search) declares `torch>=1.11.0` in its own
installed metadata. Removing torch's `+cpu` pin does not remove torch — pip still
resolves it as a transitive dependency of sentence-transformers, but now via the
default PyPI index, which serves the CUDA-linked build. That makes the "lite" CPU
image LARGER, with nvidia wheels on a host that never has a GPU.

This test asserts the invariant directly against the file: whenever
`requirements-lite.txt` declares a package known to require torch, it must ALSO
declare `torch` itself, pinned with an explicit `+cpu` local version — the only
pin shape that stops pip resolving the CUDA wheel from PyPI's default index.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LITE_REQUIREMENTS = REPO_ROOT / "backend" / "requirements-lite.txt"

#: Packages in requirements-lite.txt known (from installed dist-info METADATA) to
#: require torch transitively. Extend this set if another such package is added.
TORCH_REQUIRING_PACKAGES = {"sentence-transformers", "pyannote.audio", "torchaudio"}

_CPU_PIN_RE = re.compile(r"^torch==\S+\+cpu\s*$")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _code_lines(path: Path) -> list[str]:
    return [
        line.split("#")[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#")[0].strip()
    ]


def _declares(lines: list[str], package: str) -> bool:
    target = _normalise(package)
    for line in lines:
        if line.startswith("-"):
            continue
        name = re.split(r"[=<>~; ]", line, maxsplit=1)[0]
        if _normalise(name) == target:
            return True
    return False


def _torch_cpu_pin_present(lines: list[str]) -> bool:
    return any(_CPU_PIN_RE.match(line) for line in lines)


def test_torch_carries_an_explicit_cpu_pin_whenever_a_torch_requiring_package_is_present():
    lines = _code_lines(LITE_REQUIREMENTS)
    present = [pkg for pkg in TORCH_REQUIRING_PACKAGES if _declares(lines, pkg)]
    assert present, (
        "none of the known torch-requiring packages are declared in "
        "requirements-lite.txt — TORCH_REQUIRING_PACKAGES is stale, update it "
        "rather than let this pass on zero matches"
    )
    assert _torch_cpu_pin_present(lines), (
        f"requirements-lite.txt declares {present} (which require torch) but has "
        f"no explicit 'torch==<ver>+cpu' pin — without it, pip resolves the "
        f"default (CUDA-linked) PyPI wheel, making the CPU-only lite image LARGER, "
        f"not smaller (issue #660 §0.1)"
    )


def test_the_extra_index_url_is_still_present_alongside_the_cpu_pin():
    """`--extra-index-url https://download.pytorch.org/whl/cpu` is what makes the
    `+cpu` pin resolvable at all; dropping it while keeping the pin fails the
    install outright rather than silently resolving the wrong wheel."""
    lines = _code_lines(LITE_REQUIREMENTS)
    assert any("download.pytorch.org/whl/cpu" in line for line in lines), (
        "requirements-lite.txt no longer declares the PyTorch CPU extra index — "
        "the torch==...+cpu pin cannot resolve without it"
    )


def test_guard_fires_on_a_synthetic_file_with_the_cpu_pin_removed(tmp_path: Path) -> None:
    """Must-fire control: this is what issue #660 §0.1's mistake would actually look
    like on disk — torch present but unpinned-to-cpu, right after a torch-requiring
    package."""
    synthetic = tmp_path / "requirements-lite.txt"
    synthetic.write_text(
        "--extra-index-url https://download.pytorch.org/whl/cpu\n"
        "torch\n"  # the mistake: no +cpu pin at all
        "sentence-transformers==6.0.0\n"
    )
    lines = _code_lines(synthetic)
    present = [pkg for pkg in TORCH_REQUIRING_PACKAGES if _declares(lines, pkg)]
    assert present
    assert not _torch_cpu_pin_present(lines), (
        "the synthetic fixture was supposed to lack a +cpu pin — fixture is wrong"
    )


def test_guard_stays_clean_on_the_real_file() -> None:
    """Must-stay-clean control, run explicitly so a rule too broad to ever pass
    would be caught here rather than only in the primary test above."""
    lines = _code_lines(LITE_REQUIREMENTS)
    assert _torch_cpu_pin_present(lines)


@pytest.mark.parametrize("removed", ["pyannote.audio", "torchaudio"])
def test_step7_may_remove_pyannote_and_torchaudio_without_tripping_the_guard(
    removed: str, tmp_path: Path
) -> None:
    """After Step 7 removes pyannote.audio and torchaudio, torch stays required
    (by sentence-transformers) and the guard must still pass."""
    synthetic = tmp_path / "requirements-lite.txt"
    synthetic.write_text(
        "--extra-index-url https://download.pytorch.org/whl/cpu\n"
        "torch==2.11.0+cpu\n"
        "sentence-transformers==6.0.0\n"
    )
    lines = _code_lines(synthetic)
    present = [pkg for pkg in TORCH_REQUIRING_PACKAGES if _declares(lines, pkg)]
    assert removed not in present  # already absent from this synthetic post-shrink file
    assert _torch_cpu_pin_present(lines)
