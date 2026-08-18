"""The lock must describe the image, and the venv must match it (issue #492).

`requirements.txt` pins almost nothing: 61 floors against 4 exact `==` specs. A
floor is a version number but **not a pin** — `nltk>=3.9.4` permits 3.9.4 *or*
3.10.3 — so two installs performed at different times legitimately resolve to
different versions, and did. Measured between the host venv (where the pre-merge
gate runs) and the running backend container: **120 packages apart, 18 at a MAJOR
version** (starlette 0.48 vs 1.6, openai 2.44 vs 3.2, pandas 2.2 vs 3.0).

That is not a hypothetical: it is how the NLTK `pathsec` breakage reached
production unseen. `nltk>=3.9.4` gave the venv 3.9.4 and the image 3.10.3, and
NLTK ≥3.10 refuses multiply-linked files, so `split_sentences_nltk` raised on
every call in the backend and both transcription workers **while the host suite
passed**. The gate was structurally incapable of seeing it.

These tests are the cheap, always-on half of the fix. The expensive half — build
the image, create a fresh venv from the lock, diff `pip list --format=freeze` —
is what `scripts/lock-backend-deps.sh --check` does against a running container.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCK = REPO_ROOT / "backend" / "requirements.lock.txt"
DIRECT = REPO_ROOT / "backend" / "requirements.txt"
NODEPS = REPO_ROOT / "backend" / "requirements-nodeps.txt"
DOCKERFILE_PROD = REPO_ROOT / "backend" / "Dockerfile.prod"

#: Installed by their own `--no-deps` step, so they must NOT be in the lock — but
#: their transitive dependencies must, because `--no-deps` installs none of them.
NODEPS_PACKAGES = {"whisperx", "faster-whisper", "gliner"}

#: The installer itself. It differs between a venv and an image by construction,
#: which is why the reproducibility check excludes it too.
TOOLING = {"pip", "setuptools", "wheel"}


def _specs(path: Path) -> list[str]:
    return [
        line.split("#")[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#")[0].strip() and not line.startswith("#")
    ]


def _normalise(name: str) -> str:
    """PEP 503 normalisation: dots count too (`pyannote.audio` -> `pyannote-audio`)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _marker_excludes_linux_x86_64(marker: str) -> bool:
    """Whether an environment marker rules OUT the platform the lock describes."""
    from packaging.markers import Marker

    try:
        return not Marker(marker.strip()).evaluate(
            {"sys_platform": "linux", "platform_machine": "x86_64"}
        )
    except Exception:  # noqa: BLE001 — an unparseable marker is not this test's subject
        return False


def _locked_names() -> dict[str, str]:
    """``normalised name -> spec`` for every pin in the lock."""
    names: dict[str, str] = {}
    for spec in _specs(LOCK):
        if spec.startswith("-"):
            continue
        if " @ " in spec:
            names[_normalise(spec.split(" @ ", 1)[0])] = spec
        elif "==" in spec:
            names[_normalise(spec.split("==", 1)[0])] = spec
    return names


def test_the_lock_exists_and_is_not_trivially_small():
    """A truncated lock would silently under-constrain the install."""
    assert LOCK.is_file(), "backend/requirements.lock.txt is missing"
    assert len(_locked_names()) > 200, (
        "the lock has suspiciously few pins — it should hold the FULL resolved tree, "
        "not just the direct dependencies"
    )


def test_every_lock_spec_is_exact():
    """A floor in the lock defeats the entire point of having one."""
    loose = [
        spec
        for spec in _specs(LOCK)
        if not spec.startswith("-") and " @ " not in spec and "==" not in spec
    ]
    assert not loose, f"the lock carries specs that are not exact pins: {loose}"

    floors = [spec for spec in _specs(LOCK) if ">=" in spec or "~=" in spec]
    assert not floors, f"the lock carries version FLOORS, which pin nothing: {floors}"


def test_the_cuda_index_is_declared():
    """``torch==2.11.0+cu128`` does not exist on PyPI.

    Dropping this line turns a reproducible install into a resolver error — or
    worse, silently installs the CPU wheel. It must be `--extra-index-url`, never
    `--index-url`, which would replace PyPI entirely and break everything else.
    """
    text = LOCK.read_text(encoding="utf-8")
    assert "--extra-index-url https://download.pytorch.org/whl/cu128" in text, (
        "the cu128 index is missing, so the pinned torch build cannot resolve"
    )
    assert "\n--index-url" not in text, (
        "--index-url REPLACES PyPI; the cu128 wheels must be an EXTRA index"
    )


def test_the_git_dependency_is_pinned_to_a_commit_not_a_branch():
    """`@gpu-optimizations` meant "whatever that branch is at build time".

    A branch pin is the specific defect #492 names: the lock would be exact for
    277 packages and unpinned for the one that carries our own GPU changes.
    """
    locked = _locked_names()
    spec = locked.get("pyannote-audio") or locked.get("pyannote.audio")
    assert spec, f"pyannote.audio is not in the lock at all: {sorted(locked)[:5]}…"
    assert "git+" in spec, f"pyannote.audio lost its git source: {spec}"

    revision = spec.rsplit("@", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{40}", revision), (
        f"pyannote.audio is pinned to {revision!r}, which is not a full commit SHA. "
        "A branch or tag can move under a rebuild, which is exactly the drift this "
        "lock exists to stop."
    )


def test_the_spacy_model_is_pinned_by_url():
    """``en_core_web_sm`` is NOT on PyPI — a freeze-derived pin is uninstallable.

    Found the hard way: `pip install -r` on a naive lock failed outright with
    "No matching distribution found for en_core_web_sm==3.8.0". It ships as a
    GitHub release wheel, and `python -m spacy download` picks whichever version
    is compatible with the installed spaCy AT BUILD TIME — the same unpinned
    drift, one layer down.
    """
    spec = _locked_names().get("en-core-web-sm")
    assert spec, "en_core_web_sm is absent from the lock; Presidio needs it to tokenize"
    assert spec.startswith("en_core_web_sm @ https://"), (
        f"en_core_web_sm must be pinned by release URL, not by version: {spec}"
    )
    assert "==" not in spec.split(" @ ", 1)[1], "a bare version here does not resolve from PyPI"


def test_the_nodeps_packages_are_absent_from_the_lock():
    """They install in a separate ``--no-deps`` step and must not be duplicated.

    Installing them here would drag in their unconditional `onnxruntime`, which
    writes into the same import namespace as the pinned `onnxruntime-gpu` — and
    whichever lands last silently wins, dropping CUDAExecutionProvider with no
    error. That is the whole reason `requirements-nodeps.txt` exists.
    """
    locked = _locked_names()
    intruders = sorted(NODEPS_PACKAGES & set(locked))
    assert not intruders, f"{intruders} are installed --no-deps and must not also be in the lock"


def test_the_nodeps_step_is_still_pinned_exactly():
    """The second install step is not covered by the lock, so it pins its own."""
    loose = [spec for spec in _specs(NODEPS) if "==" not in spec]
    assert not loose, f"requirements-nodeps.txt must pin exactly: {loose}"


def test_installer_tooling_is_excluded():
    """`pip`/`setuptools` differ between a venv and an image by construction.

    Pinning them would make the reproducibility diff fail forever on a difference
    that means nothing — and a check that always fails gets ignored.
    """
    present = sorted(TOOLING & set(_locked_names()))
    assert not present, f"the lock pins installer tooling, which cannot match: {present}"


def test_the_production_image_installs_from_the_lock():
    """A lock nothing installs from is decoration.

    This is the half that makes the gate honest: the venv and the image must run
    the same two commands over the same two files.
    """
    text = DOCKERFILE_PROD.read_text(encoding="utf-8")
    assert "-r requirements.lock.txt" in text, (
        "Dockerfile.prod does not install from the lock, so the image and the venv "
        "can still resolve differently"
    )
    assert not re.search(r"pip install[^\n]*-r requirements\.txt", text), (
        "Dockerfile.prod still installs from the unpinned requirements.txt"
    )
    # Match the RUN, not the word: this file now EXPLAINS why `spacy download` is
    # gone, and a substring check fired on that explanation.
    assert not re.search(r"^RUN[^\n]*spacy download", text, re.M), (
        "`spacy download` resolves a model version at build time; the lock pins it "
        "by URL instead, so this step would undo the pin"
    )


def test_direct_dependencies_are_all_represented_in_the_lock():
    """Every DIRECT dependency must appear, or the lock under-installs.

    Guards the generator against a filter that is too broad — the `--no-deps` and
    tooling exclusions are narrow on purpose, and widening one would silently drop
    a real dependency from every build.
    """
    locked = set(_locked_names())
    missing = []
    for spec in _specs(DIRECT):
        if spec.startswith("-"):
            continue
        # ⚠️ Skip specs whose environment marker EXCLUDES the platform the lock was
        # frozen on. `requirements.txt` carries an onnxruntime pair —
        # `onnxruntime-gpu; linux and x86_64` and `onnxruntime; NOT linux/x86_64` —
        # and the image is linux/x86_64, so the CPU fallback is correctly absent.
        # Reporting it as missing was this test misreading a resolved marker as a
        # gap. See the lock's own header: it is a linux/x86_64 lock by construction.
        if ";" in spec and _marker_excludes_linux_x86_64(spec.split(";", 1)[1]):
            continue
        name = re.split(r"[<>=!;\[ @]", spec, maxsplit=1)[0].strip()
        if not name:
            continue
        normalised = _normalise(name)
        if normalised in NODEPS_PACKAGES or normalised in TOOLING:
            continue
        if normalised not in locked:
            missing.append(name)

    assert not missing, (
        "these direct dependencies of requirements.txt are absent from the lock, so "
        f"a build from the lock would not install them: {sorted(missing)}"
    )


@pytest.mark.parametrize("package", ["torch", "torchaudio", "nltk", "starlette", "openai"])
def test_the_packages_that_actually_drifted_are_pinned(package: str):
    """Named explicitly, because these are the ones measured as divergent.

    `nltk` is the one that broke production; `starlette`'s threadpool behaviour is
    what #485 turned on. A regression here is not abstract.
    """
    spec = _locked_names().get(_normalise(package))
    assert spec and "==" in spec, f"{package} is not exactly pinned in the lock: {spec!r}"


def test_the_ci_requirements_do_not_contradict_the_lock():
    """CI is a different tree, but it must not pin a DIFFERENT version.

    `requirements-ci.txt` installs CPU-only wheels so the GitHub job needs no CUDA
    (~200 MB against ~3 GB), so its torch differs from the lock's by the local
    version tag `+cu128` — that is the point of the file, not drift. What would be
    drift is a different upstream version: CI validating torch 2.12 while the image
    runs 2.11 puts the safety net on a different program from the thing it nets.

    ⚠️ Known remaining work on #492: `requirements-ci.txt` is itself 63 floors
    against 3 exact pins, so it has the same unpinned problem this lock fixes for
    the image. Locking it needs its own CPU-resolved tree, which cannot be derived
    from the GPU image the generator reads — deliberately out of scope here rather
    than half-done.
    """
    ci: dict[str, str] = {}
    for spec in _specs(REPO_ROOT / "backend" / "requirements-ci.txt"):
        if spec.startswith("-") or "==" not in spec:
            continue
        name, version = spec.split("==", 1)
        ci[_normalise(name)] = version.split(";")[0].strip()

    locked = _locked_names()
    conflicts = []
    for name, ci_version in ci.items():
        # Not `spec`: that name is already bound as `str` by the loop above, and
        # rebinding it to `str | None` here is what mypy caught.
        locked_spec = locked.get(name)
        if not locked_spec or "==" not in locked_spec:
            continue
        lock_version = locked_spec.split("==", 1)[1]
        # A local version tag (`+cu128`) is the CPU/GPU build difference, not drift.
        if lock_version.split("+", 1)[0] != ci_version.split("+", 1)[0]:
            conflicts.append(f"{name}: ci={ci_version} lock={lock_version}")

    assert not conflicts, (
        "requirements-ci.txt pins a different upstream version from the lock, so CI "
        f"validates a different program from the one that ships: {conflicts}"
    )
