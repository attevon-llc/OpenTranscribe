"""The validated OpenBLAS must travel WITH ``diar-server`` (issue #721).

WHY THIS EXISTS
---------------
``diar-server`` is built ``openblas-system``: it carries an ELF ``NEEDED`` entry for
``libopenblas.so.0`` and links whatever the **host image** provides. Upstream validates on
Ubuntu 24.04's **0.3.26**; both runtime stages here are ``python:3.13-slim-trixie``, whose
``libopenblas0`` is an **unpinned 0.3.29+ds-3**. Upstream reports an **arm64-only**
GEMM->GEMV forwarding defect in 0.3.28/0.3.29 that moved its AMI-16 DER from **13.8% to
48.7%** — with ``diar-server verify-models`` passing all five stages throughout, so a
plausible speaker count sits on badly wrong attribution.

⚠️ **That defect does not reproduce on hardware available to this project**, and this module
must not be read as guarding against a live 48.7% regression. Measured 2026-09-05: amd64 and
**native aarch64** (Apple M2 Max) both scored **0.0669 DER on 0.3.29**, identical to a 0.3.26
control, and a 2,016-case ``cblas_?gemm`` probe over the forwarding shapes found 0
mismatches. What the bundling buys is (a) removal of **unpinned drift** — nothing in this
repo pins ``libopenblas0``, so the next Debian bump silently re-forms the pairing — and (b)
coverage for **Graviton3/4**, which select SVE kernels no available hardware can execute
(forcing them SIGILLs) and which therefore remain unproven (#713).

Either way the mechanism needs a test rather than a comment, because **nothing else in the
repo can see it move**: no image scan, no build gate, no healthcheck, and specifically not
``tests/integration/test_boundary_regression.py``, which replays frozen ``*.rawinfer.json``
inference and never runs the diarizer at all.

WHAT IT ASSERTS, AND THE FAILURE IT IS SHAPED AROUND
-----------------------------------------------------
Copying a library into an image is easy; making the loader *choose* it is the part that
silently no-ops. A Dockerfile that ships ``libopenblasp-r0.3.26.so`` but whose process still
maps ``libopenblasp-r0.3.29.so`` looks **exactly like success** — same files, same build
log, same green tests. So the structural half checked here is deliberately the *pairing*:
the library COPY **and** the ``patchelf --force-rpath`` that makes it load, **and** that the
shipped binary is the patched one rather than upstream's.

``--force-rpath`` is asserted specifically. It emits **DT_RPATH**, which the loader searches
*before* ``LD_LIBRARY_PATH``; patchelf's default emits DT_RUNPATH, which is searched *after*
it. Only the former makes the validated pairing impossible to displace with an environment
variable — the same failure class as the bug itself.

The runtime proof that the loader really resolves the bundled copy lives in
``tests/integration/test_diar_native_openblas_runtime.py``, which reads
``/proc/<pid>/maps`` out of a real container. This module is the fast, always-on guard that
the mechanism is still wired at all; that one is the evidence it works.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]

#: The two images that ship ``diar-server``. ``Dockerfile.lite`` is where the concern was
#: concentrated — ``docker-build-push.sh list-platforms`` reports it as the only component
#: with a ``linux/arm64`` leg, and Debian trixie's arm64 archive serves the same 0.3.29+ds-3.
DOCKERFILES = ("Dockerfile.prod", "Dockerfile.lite")

#: The stage that pairs upstream's binary with upstream's library.
STAGED_STAGE = "diar-native-staged"

#: The per-``TARGETARCH`` stage both the binary and the library must come from, so the
#: library's architecture matches the binary's by construction and cannot drift.
ARCH_STAGE = "diar-native-bin"

#: Where the bundled library lives, and the only directory the patched RPATH names.
PRIVATE_LIB_DIR = "/opt/diar-native/lib"

#: First OpenBLAS release carrying the arm64 GEMM->GEMV forwarding defect upstream reports.
#: Bundling a version at or above this would put the shipped artifact back inside the window
#: #721 is about -- and would be pointless, since the whole point of bundling is to pin the
#: version upstream validated on rather than track whatever the base image installs.
FIRST_DEFECTIVE_VERSION = (0, 3, 28)

_VERSIONED_SO = re.compile(r"libopenblasp-r(\d+)\.(\d+)\.(\d+)\.so")


def _logical_lines(path: Path) -> list[str]:
    """Dockerfile text with backslash-continuations folded into single logical lines.

    Every assertion below is about one *instruction*, but the real Dockerfiles wrap their
    ``RUN``/``COPY`` instructions across many physical lines. Matching per physical line
    would make ``patchelf --force-rpath --set-rpath /opt/diar-native/lib`` unmatchable and
    the test would pass by never looking at it.
    """
    text = path.read_text(encoding="utf-8")
    folded = re.sub(r"\\\s*\n\s*", " ", text)
    return [
        line for line in folded.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _instructions(name: str) -> list[str]:
    path = _BACKEND / name
    assert path.is_file(), f"not found: {path}"
    return _logical_lines(path)


def _staged_section(name: str) -> list[str]:
    """The instructions belonging to the ``diar-native-staged`` stage only."""
    lines = _instructions(name)
    starts = [
        i for i, line in enumerate(lines) if re.match(rf"FROM\s+\S+\s+AS\s+{STAGED_STAGE}\b", line)
    ]
    assert len(starts) == 1, (
        f"{name}: expected exactly one `FROM ... AS {STAGED_STAGE}` stage, found {len(starts)}. "
        f"That stage is where upstream's validated OpenBLAS is paired with upstream's binary "
        f"(issue #721); without it the shipped binary links whatever the runtime image's apt "
        f"`libopenblas0` happens to be — an UNPINNED 0.3.29 on trixie today, and whatever "
        f"the next Debian bump installs after that."
    )
    start = starts[0]
    ends = [i for i in range(start + 1, len(lines)) if lines[i].startswith("FROM ")]
    return lines[start : ends[0] if ends else len(lines)]


def _bundled_versions(name: str) -> list[tuple[int, int, int]]:
    """Every ``libopenblasp-rX.Y.Z.so`` version named anywhere in the Dockerfile."""
    return [
        (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        for line in _instructions(name)
        for m in _VERSIONED_SO.finditer(line)
    ]


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_validated_openblas_is_copied_from_the_same_per_arch_stage(name: str) -> None:
    """The library must come out of ``diar-native-bin``, not be re-fetched from apt.

    Sourcing it from the SAME per-``TARGETARCH`` stage the binary comes from is what makes
    the aarch64 leg get the aarch64 0.3.26 automatically. An apt pin would have to name a
    version per suite and could drift from the binary independently.
    """
    section = _staged_section(name)
    copies = [
        line
        for line in section
        if line.startswith("COPY ")
        and f"--from={ARCH_STAGE}" in line
        and _VERSIONED_SO.search(line)
        and "/openblas-pthread/" in line
    ]
    assert len(copies) == 1, (
        f"{name}: expected exactly one COPY of a versioned libopenblasp-rX.Y.Z.so out of "
        f"`--from={ARCH_STAGE}`'s /usr/lib/*/openblas-pthread/ in the `{STAGED_STAGE}` stage, "
        f"found {len(copies)}. That COPY is what makes the validated library travel with the "
        f"binary; without it `diar-server` falls back to the runtime image's apt libopenblas0. "
        f"Two of them would mean two candidate libraries and no stated winner. Section was:\n"
        + "\n".join(section)
    )
    # The glob, not a hardcoded triplet: one Dockerfile builds both linux/amd64 and
    # linux/arm64, so `x86_64-linux-gnu` spelled out would silently copy nothing on the
    # arm64 leg -- and BuildKit fails a COPY that matches no source, so this stays honest.
    assert "/usr/lib/*/openblas-pthread/" in copies[0], (
        f"{name}: the OpenBLAS COPY source is not the multiarch glob "
        f"/usr/lib/*/openblas-pthread/:\n    {copies[0]}\n"
        f"A hardcoded multiarch triplet cannot serve both the amd64 and arm64 legs."
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_binary_is_patched_with_dt_rpath_not_dt_runpath(name: str) -> None:
    """``--force-rpath`` specifically: DT_RUNPATH loses to ``LD_LIBRARY_PATH``, DT_RPATH wins.

    Dropping ``--force-rpath`` would still "work" on every deployment that happens not to set
    a competing ``LD_LIBRARY_PATH`` — i.e. it would pass every test and every smoke check
    while quietly making the guarantee defeasible by an env var.
    """
    section = _staged_section(name)
    patchelf_lines = [line for line in section if "patchelf --force-rpath" in line]
    assert len(patchelf_lines) == 1, (
        f"{name}: expected exactly one `patchelf --force-rpath` instruction in the "
        f"`{STAGED_STAGE}` stage, found {len(patchelf_lines)}. Without DT_RPATH the bundled "
        f"library is present but NOT loaded — an image that ships 0.3.26 and still runs "
        f"0.3.29 is indistinguishable from a working one. `--force-rpath` is not optional: "
        f"patchelf's default writes DT_RUNPATH, which the loader searches AFTER "
        f"LD_LIBRARY_PATH. Section was:\n" + "\n".join(section)
    )
    assert f"--set-rpath {PRIVATE_LIB_DIR}" in patchelf_lines[0], (
        f"{name}: patchelf does not set the RPATH to {PRIVATE_LIB_DIR}:\n"
        f"    {patchelf_lines[0]}\n"
        f"That is the only directory holding the bundled libopenblas.so.0; any other value "
        f"sends the loader back to the ld cache and the distro's copy."
    )
    assert "--print-rpath" in patchelf_lines[0], (
        f"{name}: the patchelf instruction never reads the RPATH back:\n"
        f"    {patchelf_lines[0]}\n"
        f"A patchelf that silently fails to apply must fail the BUILD. Without the read-back "
        f"`test`, the result is an image that ships the library and ignores it — which is the "
        f"exact silent no-op issue #721 is about, reintroduced at build time."
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_shipped_binary_is_the_patched_one(name: str) -> None:
    """The runtime stage must take ``diar-server`` from the staged stage, never upstream's.

    This is the regression this module exists for. Reverting the runtime COPY back to
    ``--from=diar-native-bin`` is a one-token change that leaves the library COPY, the
    symlink and every comment intact, builds clean, and silently ships an unpatched binary
    that maps the distro's 0.3.29.
    """
    runtime_copies = [
        line
        for line in _instructions(name)
        if line.startswith("COPY ") and line.rstrip().endswith("/usr/local/bin/diar-server")
    ]
    assert len(runtime_copies) == 1, (
        f"{name}: expected exactly one COPY into /usr/local/bin/diar-server, "
        f"found {len(runtime_copies)}: {runtime_copies}"
    )
    assert f"--from={STAGED_STAGE}" in runtime_copies[0], (
        f"{name}: /usr/local/bin/diar-server is copied from a stage other than "
        f"`{STAGED_STAGE}`:\n    {runtime_copies[0]}\n"
        f"Only the staged copy carries DT_RPATH={PRIVATE_LIB_DIR}. Taking the binary "
        f"straight from `{ARCH_STAGE}` again re-links it against the runtime image's "
        f"apt libopenblas0 (0.3.29 on trixie) while leaving the bundled 0.3.26 sitting "
        f"unused in the image — the exact silent no-op issue #721 is about."
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_soname_symlink_points_at_the_bundled_version(name: str) -> None:
    """``diar-server``'s DT_NEEDED asks for ``libopenblas.so.0``; something must provide it.

    The link target keeps the version in its filename on purpose, so ``/proc/<pid>/maps``
    names the version that actually ran rather than an opaque ``libopenblas.so.0``.
    """
    pattern = re.compile(
        rf"ln -s (libopenblasp-r\d+\.\d+\.\d+\.so) {re.escape(PRIVATE_LIB_DIR)}/libopenblas\.so\.0"
    )
    targets = [m.group(1) for line in _instructions(name) for m in pattern.finditer(line)]
    assert len(targets) == 1, (
        f"{name}: expected exactly one "
        f"`ln -s libopenblasp-rX.Y.Z.so {PRIVATE_LIB_DIR}/libopenblas.so.0`, found "
        f"{len(targets)}: {targets}. DT_RPATH points the loader at {PRIVATE_LIB_DIR}, but the "
        f"NEEDED soname is `libopenblas.so.0` — with no such name in that directory the "
        f"loader falls through to the ld cache and picks the distro's 0.3.29 with no error."
    )
    copied = {
        f"libopenblasp-r{major}.{minor}.{patch}.so"
        for major, minor, patch in _bundled_versions(name)
    }
    assert targets[0] in copied, (
        f"{name}: the soname link targets {targets[0]}, which no COPY in this file produces "
        f"({sorted(copied)}). `ln -s` does not require its target to exist, so the image "
        f"builds clean with a dangling link and the loader silently falls back to the distro's "
        f"OpenBLAS."
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_every_openblas_version_named_in_the_file_is_the_same_one(name: str) -> None:
    """A half-landed version bump ships a COPY and a symlink that disagree.

    The symlink target is a plain string: if it names a file the COPY no longer produces,
    the image builds fine (``ln -s`` does not require its target to exist) and the dangling
    link sends the loader straight back to the distro copy.
    """
    versions = _bundled_versions(name)
    assert versions, f"{name}: names no libopenblasp-rX.Y.Z.so at all"
    assert len(set(versions)) == 1, (
        f"{name} names more than one OpenBLAS version: {sorted(set(versions))}. The COPY "
        f"source, the runtime COPY and the `ln -s` target must all be the same file, or the "
        f"soname link dangles and the loader silently falls back to the distro's copy."
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_bundled_version_predates_the_arm64_gemm_defect(name: str) -> None:
    """This is the actual invariant — 0.3.28/0.3.29 are the versions upstream implicates.

    Asserting only "some version is bundled" would keep passing if someone bundled the
    distro's 0.3.29 to "match the image", which throws away the entire point: the bundled
    copy exists to be the version diar-native validated on, independent of the base image.
    """
    bundled = set(_bundled_versions(name))
    assert bundled, f"{name}: names no libopenblasp-rX.Y.Z.so at all"
    offenders = sorted(v for v in bundled if v >= FIRST_DEFECTIVE_VERSION)
    assert not offenders, (
        f"{name} bundles OpenBLAS {offenders}, at or past "
        f"{'.'.join(map(str, FIRST_DEFECTIVE_VERSION))} — the release upstream root-causes "
        f"its arm64-only GEMM->GEMV report to (AMI-16 DER 13.8% -> 48.7%, with "
        f"`verify-models` passing throughout). Bundle the version diar-native validates on."
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_apt_libopenblas_install_is_still_present(name: str) -> None:
    """Removing it looks like harmless deduplication and is not (issue #721).

    Measured in the built image: numpy and scipy never touch the system copy (they load
    their own vendored builds), and an ELF sweep found ``diar-server`` is its only consumer
    outside its own package — so dropping it *looks* free. But the same package set provides
    the ``libblas.so.3``/``liblapack.so.3`` alternatives that ffmpeg's ``libsphinxbase`` links,
    and it is what pulls in ``libgfortran5``, which the **bundled** 0.3.26 itself needs.
    """
    installs = [line for line in _instructions(name) if "apt-get install" in line]
    packages = " ".join(installs)
    assert "libopenblas0" in packages, (
        f"{name}: no longer apt-installs libopenblas0. That package pulls in libgfortran5, "
        f"which the BUNDLED libopenblasp-r*.so needs as its only non-glibc NEEDED entry — "
        f"removing it makes the bundled library unloadable — and it provides the "
        f"libblas.so.3/liblapack.so.3 alternatives ffmpeg's libsphinxbase links against. "
        f"apt-get install lines found were:\n" + "\n".join(installs)
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_private_lib_dir_is_never_put_on_an_image_wide_ld_library_path(name: str) -> None:
    """The scoping constraint that made DT_RPATH the right mechanism in the first place.

    One shared backend image runs the API, every Celery worker and the diar-native sidecar.
    An image-wide ``LD_LIBRARY_PATH`` naming this directory would hand diar's ORT 1.24.2
    provider libs — same filenames as Python's onnxruntime-gpu 1.28.0 — to every Python
    process in the image, and would put the bundled OpenBLAS ahead of the system one for
    processes that never asked for it.
    """
    offenders = [
        line
        for line in _instructions(name)
        if line.startswith("ENV ") and "LD_LIBRARY_PATH=" in line and PRIVATE_LIB_DIR in line
    ]
    assert not offenders, (
        f"{name} puts {PRIVATE_LIB_DIR} on an image-wide ENV LD_LIBRARY_PATH:\n"
        + "\n".join(offenders)
        + "\nScope it to the diar-native compose service instead (see "
        "docker-compose.diar-native.yml); the binary already carries DT_RPATH for the "
        "invocations compose does not cover."
    )
