"""Every requirements file must pin exactly, so the venv equals the container (#492).

`requirements.txt` was **62 floors against 5 exact pins**, and a floor is a version
number but **not a pin**: `nltk>=3.9.4` permits 3.9.4 *or* 3.10.3. Two installs
performed at different times therefore resolve differently — and did. Measured
between the host venv (where the pre-merge gate runs) and the running backend
container: **120 packages apart, 18 at a MAJOR version** (starlette 0.48 vs 1.6,
openai 2.44 vs 3.2, pandas 2.2 vs 3.0).

That is not hypothetical. It is how the NLTK `pathsec` breakage reached production
unseen: the venv got 3.9.4 and the image 3.10.3, and NLTK ≥3.10 refuses
multiply-linked files, so `split_sentences_nltk` raised on every call in the
backend and both transcription workers **while the host suite passed**. The gate
was structurally incapable of seeing it.

There is deliberately **no separate lock file**. Each environment has one
requirements file, fully pinned, installed by both its container and (for the prod
one) the dev venv — so "what a developer installs" and "what ships" are the same
text, not two artefacts that can disagree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"

#: Every requirements file, and what installs it. A new one must be added here —
#: an unpinned file that nothing checks is exactly how this regressed.
REQUIREMENTS = {
    "requirements.txt": "Dockerfile.prod, Dockerfile.blackwell, the dev venv",
    "requirements-nodeps.txt": "the same, as a second --no-deps step",
    "requirements-lite.txt": "Dockerfile.lite (CPU-only image)",
    "requirements-ci.txt": "GitHub Actions (CPU-only)",
    "requirements-dev.txt": "the venv only (pytest, linters, pre-commit)",
}

#: Installed `--no-deps` from their own file. They must not be pinned anywhere
#: else: installing them normally drags in an unconditional `onnxruntime` that
#: writes into the same import namespace as the pinned `onnxruntime-gpu`, and
#: whichever lands last silently wins — dropping CUDAExecutionProvider with no
#: error.
NODEPS_PACKAGES = {"whisperx", "faster-whisper", "gliner"}


def _specs(name: str) -> list[str]:
    """Code-bearing lines of a requirements file, comments stripped."""
    path = BACKEND / name
    return [
        line.split("#")[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#")[0].strip()
    ]


def _normalise(name: str) -> str:
    """PEP 503 normalisation — dots fold too (`pyannote.audio` -> `pyannote-audio`)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(name: str) -> dict[str, str]:
    """``normalised name -> spec`` for every requirement in a file."""
    found: dict[str, str] = {}
    for spec in _specs(name):
        if spec.startswith("-"):
            continue
        if " @ " in spec:
            found[_normalise(spec.split(" @ ", 1)[0])] = spec
        elif "==" in spec:
            found[_normalise(re.sub(r"\[.*?\]", "", spec.split("==", 1)[0]))] = spec
    return found


@pytest.mark.parametrize("name", sorted(REQUIREMENTS))
def test_every_spec_is_pinned(name: str) -> None:
    """A floor is a version number but not a pin, and that is the whole defect."""
    unpinned = [
        spec
        for spec in _specs(name)
        if not spec.startswith("-") and "==" not in spec and " @ " not in spec
    ]
    assert not unpinned, (
        f"{name} ({REQUIREMENTS[name]}) carries specs that are not exact pins, so two "
        f"installs at different times can resolve differently: {unpinned}"
    )


@pytest.mark.parametrize("name", sorted(REQUIREMENTS))
def test_no_open_ended_ranges(name: str) -> None:
    """`>=`/`~=` anywhere means the file does not describe one environment."""
    ranged = [spec for spec in _specs(name) if re.search(r"(>=|~=|>|<=|<)", spec)]
    # A `; python_version < "3.13"` style MARKER is not a version range on the
    # package itself — strip markers before judging.
    ranged = [spec for spec in ranged if re.search(r"(>=|~=|>|<=|<)", spec.split(";")[0])]
    assert not ranged, f"{name} still carries version ranges: {ranged}"


def test_the_spacy_model_is_declared_in_requirements_not_a_dockerfile() -> None:
    """A model the app cannot run without belongs in a requirements file.

    ``en_core_web_sm`` is the spaCy pipeline Presidio tokenizes with, and it used to
    be installed by ``RUN python -m spacy download`` in ``Dockerfile.prod`` **only**.
    That broke the venv-equals-container rule in both directions: `spacy download`
    resolves whichever version suits the installed spaCy AT BUILD TIME (so the one
    thing Presidio cannot work without was the one thing nothing pinned), and
    ``Dockerfile.blackwell`` — which installs the same requirements.txt — had no
    such step at all, so that image shipped PII redaction with no pipeline to
    tokenize with.
    """
    spec = _requirement_names("requirements.txt").get("en-core-web-sm")
    assert spec, (
        "en_core_web_sm is not declared in requirements.txt; Presidio has no spaCy "
        "pipeline to tokenize with in any image that does not run `spacy download`"
    )
    assert spec.startswith("en_core_web_sm @ https://"), (
        f"spaCy models are not published to PyPI, so this must be a release URL: {spec}"
    )


@pytest.mark.parametrize("dockerfile", ["Dockerfile.prod", "Dockerfile.lite"])
def test_no_dockerfile_installs_packages_outside_a_requirements_file(dockerfile: str) -> None:
    """The container must not add dependencies the venv cannot get.

    Any `RUN pip install <package>` or `RUN python -m spacy download` is a package
    that exists in the image and not in a developer's venv — which is the whole
    class of bug this module exists to prevent.

    ⚠️ ``Dockerfile.blackwell`` is deliberately EXCLUDED, and the reason is written
    down rather than assumed. It builds on ``nvcr.io/nvidia/pytorch``, whose torch
    stack it must force-reinstall from a manifest baked into the base image — a
    version list that exists only inside that image and therefore cannot live in a
    requirements file. Its other inline pins (``huggingface_hub==0.23.5``) are
    Blackwell-specific compatibility shims its own comments explain. It is an
    optional GPU variant, not the dev or prod path; giving it a reproducible
    requirements file is real remaining work, not something to fake by loosening
    this rule.
    """
    path = BACKEND / dockerfile
    if not path.is_file():
        pytest.skip(f"{dockerfile} is not present in this checkout")

    offenders = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("RUN"):
            continue
        if "spacy download" in line:
            offenders.append(line.strip())
        # `pip install` with no `-r` and no `--upgrade pip` is an inline package.
        if "pip install" in line and "-r " not in line and "--upgrade pip" not in line:
            offenders.append(line.strip())

    assert not offenders, (
        f"{dockerfile} installs packages outside a requirements file, so the image and "
        f"a venv built from those files differ: {offenders}"
    )


def test_no_dockerfile_reinstalls_a_package_at_a_different_version() -> None:
    """An inline `pip install pkg==X` after `-r requirements.txt` is a silent override.

    `Dockerfile.blackwell` ran ``pip install "huggingface_hub==0.23.5"`` *after*
    installing requirements.txt, to work around NVIDIA base-image libraries that
    passed the `use_auth_token` parameter removed in >=0.24.

    That downgrade **broke the image outright**, and nothing caught it, because pip
    reports a version conflict as a *warning* and exits 0. `transformers==4.57.6`
    declares ``huggingface-hub<1.0,>=0.34.0`` and enforces it AT IMPORT, so the
    Blackwell image could not ``import transformers`` at all — taking whisperx,
    sentence-transformers and detoxify with it. Reproduced 2026-08-18 by applying
    the downgrade to a prod image built from the same requirements.txt.

    The general rule this encodes: a Dockerfile may not name a version for a package
    that requirements.txt already pins. If the image needs a different version, the
    two pins disagree and one of them is wrong — resolve it in the requirements file
    where the whole tree is resolved together, not in a `RUN` line that only pip's
    exit code (0) sees.
    """
    pinned = _requirement_names("requirements.txt")
    offenders = []
    for path in sorted(BACKEND.glob("Dockerfile*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            # Skip COMMENTS. The first version of this detector did not, and so it
            # fired on the comment written above the removed downgrade explaining
            # what the downgrade had been — reporting the documentation of a fixed
            # bug as the bug. A scanner that cannot tell prose from instruction is
            # the same failure the Tika `Content-Type` note in the root CLAUDE.md
            # describes, pointed the other way.
            if line.lstrip().startswith("#"):
                continue
            if "pip install" not in line or "-r " in line:
                continue
            for package, version in re.findall(r'"([A-Za-z0-9_.-]+)==([^"]+)"', line):
                declared = pinned.get(_normalise(package))
                if declared and "==" in declared:
                    declared_version = declared.split("==", 1)[1].split(";")[0].strip()
                    if declared_version != version:
                        offenders.append(
                            f"{path.name}: installs {package}=={version} but requirements.txt "
                            f"pins {declared_version}"
                        )

    assert not offenders, (
        "A Dockerfile overrides a version requirements.txt pins. pip exits 0 on a "
        "dependency conflict, so this fails at import time in the built image, not at "
        "build time:\n  " + "\n  ".join(offenders)
    )


def test_the_nodeps_packages_appear_only_in_their_own_file() -> None:
    """Pinning them elsewhere would clobber `onnxruntime-gpu`."""
    # Only the GPU tree. The hazard is that installing these normally drags in an
    # unconditional `onnxruntime` that clobbers the pinned `onnxruntime-gpu` in the
    # same import namespace — and `requirements-ci.txt` is CPU-only, with no
    # `onnxruntime-gpu` to clobber, so it may pin them directly.
    for name in ("requirements.txt", "requirements-lite.txt", "requirements-dev.txt"):
        intruders = sorted(NODEPS_PACKAGES & set(_requirement_names(name)))
        assert not intruders, (
            f"{name} pins {intruders}, which must only be installed --no-deps from "
            "requirements-nodeps.txt — see that file's header"
        )


def test_the_cuda_index_is_declared_where_torch_is_pinned() -> None:
    """`torch` on the cu128 index is a different build from PyPI's.

    Dropping the index line does not fail loudly — it silently installs the CPU
    wheel, and the GPU worker then runs on the CPU.
    """
    specs = _specs("requirements.txt")
    assert any(s.startswith("--extra-index-url") and "cu128" in s for s in specs), (
        "requirements.txt pins torch but no longer declares the cu128 index"
    )
    assert not any(s.startswith("--index-url") for s in specs), (
        "--index-url REPLACES PyPI entirely; the CUDA wheels must be an EXTRA index"
    )


def test_the_git_dependency_names_an_immutable_commit() -> None:
    """A branch name is a revision, and still not a pin.

    `pyannote.audio` is our own GPU-optimised fork. `@gpu-optimizations` **looks**
    specific, which is what makes it worse than a `>=` floor: it names a moving
    branch, so two builds a week apart ship different diarization code with an
    identical requirements file. Only a commit SHA is immutable.

    ⚠️ The first version of this test asserted merely that *some* `@revision` was
    present — which `@gpu-optimizations` satisfies. It passed against the exact
    defect #492 named. A guard that accepts the thing it guards against is worse
    than no guard, because it is counted as coverage.
    """
    for source in ("requirements.txt", "Dockerfile.blackwell"):
        text = (BACKEND / source).read_text(encoding="utf-8")
        revisions = re.findall(r"pyannote-audio\.git@([\w.-]+)", text.replace("_", "-"))
        assert revisions, f"{source} no longer installs the pyannote fork from git"
        for revision in revisions:
            assert re.fullmatch(r"[0-9a-f]{40}", revision), (
                f"{source} pins the pyannote fork to {revision!r}, which is a moving ref — a "
                "rebuild takes whatever that branch head is. Pin the 40-char commit SHA."
            )


@pytest.mark.parametrize("package", ["nltk", "starlette", "torch", "openai"])
def test_the_packages_that_actually_drifted_are_pinned(package: str) -> None:
    """Named explicitly, because these are the ones measured as divergent.

    `nltk` is the one that broke production; `starlette`'s threadpool behaviour is
    what #485 turned on. A regression here is not abstract.
    """
    spec = _requirement_names("requirements.txt").get(_normalise(package))
    assert spec and "==" in spec, f"{package} is not exactly pinned: {spec!r}"


def test_ci_and_prod_do_not_pin_different_upstream_versions() -> None:
    """CI is a CPU tree, but it must not validate a DIFFERENT program.

    `requirements-ci.txt` installs CPU wheels so the GitHub job needs no CUDA
    (~200 MB against ~3 GB), so its torch differs from prod's by the local version
    tag — that is the point of the file. What would be drift is a different
    upstream version: CI validating torch 2.12 while the image runs 2.11.
    """
    prod = _requirement_names("requirements.txt")
    conflicts = []
    for name, ci_spec in _requirement_names("requirements-ci.txt").items():
        prod_spec = prod.get(name)
        if not prod_spec or "==" not in prod_spec or "==" not in ci_spec:
            continue
        ci_version = ci_spec.split("==", 1)[1].split(";")[0].strip()
        prod_version = prod_spec.split("==", 1)[1].split(";")[0].strip()
        if ci_version.split("+", 1)[0] != prod_version.split("+", 1)[0]:
            conflicts.append(f"{name}: ci={ci_version} prod={prod_version}")

    assert not conflicts, (
        "requirements-ci.txt pins a different upstream version from requirements.txt, "
        f"so CI validates a different program from the one that ships: {conflicts}"
    )
