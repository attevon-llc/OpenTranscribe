"""A test that sources ``dev-test-overlays.sh`` must go through the sealed harness.

Sourcing that library from a unit test arms two things that reach the live stack: a real
``docker ps`` scoped to the running compose project (``overlay_container_name``), and the
``trap teardown_overlays EXIT`` the library installs at source time, which ``docker stop``s
whatever that lookup named. Together they killed ``opentranscribe-mock-llm`` on every run of
the unit suite — 10 s SIGTERM grace, then SIGKILL, four minutes into a 38-minute gate, taking
6 integration and 26 e2e tests down as silent skips with it.

``tests/fixtures/overlay_lib_harness.py`` is the one sealed way to do it, and
``test_dev_test_overlay_recreate_keeps_env.py::test_docker_is_sealed`` is where that seal is
proven **behaviourally** — both halves of it were watched to fail (removing the ``PATH``
interception, and removing the ``trap - EXIT``).

This file is the routing check that keeps the next module from hand-rolling its own preamble
and re-acquiring the bug. It is deliberately a structural check, not a second behavioural one:
running the candidate modules as pytest subprocesses costs **10.7 s each** (measured — that is
interpreter start plus conftest import, before a single assertion), which is more than the whole
unit suite would tolerate for a guard.

Rule for a new module: build your script with ``sealed_script()``. If you genuinely need a
different preamble, extend the harness — do not copy it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.overlay_lib_harness import OVERLAY_LIB

UNIT_DIR = Path(__file__).resolve().parent
HARNESS_IMPORT = "tests.fixtures.overlay_lib_harness"
SEALED_BUILDER = "sealed_script"


def _runs_overlay_lib(text: str) -> bool:
    """True when ``text`` EXECUTES the overlay library, sealed or not.

    Two shapes count: a hand-rolled bash script with its own ``source`` line (the shape being
    banned), and a call to the harness's ``sealed_script`` (the shape being required). Merely
    naming the library is not enough — three modules read the file to assert on its text and
    must not be caught here.
    """
    if f"{SEALED_BUILDER}(" in text:
        return True
    if "dev-test-overlays.sh" not in text and "OVERLAY_LIB" not in text:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("source ", "source\t")):
            continue
        if "OVERLAY_LIB" in stripped or "dev-test-overlays.sh" in stripped:
            return True
    return False


def _unsealed_reason(text: str) -> str | None:
    """Why ``text`` is not sealed, or None when it routes through the harness."""
    if HARNESS_IMPORT not in text:
        return f"does not import {HARNESS_IMPORT}"
    if SEALED_BUILDER not in text:
        return f"imports the harness but never calls {SEALED_BUILDER}()"
    return None


def _candidate_files() -> list[Path]:
    return sorted(
        p
        for p in UNIT_DIR.glob("test_*.py")
        if p.name != Path(__file__).name and _runs_overlay_lib(p.read_text(encoding="utf-8"))
    )


@pytest.mark.skipif(
    not OVERLAY_LIB.exists(), reason="scripts/lib/dev-test-overlays.sh not in this checkout"
)
def test_every_module_that_sources_the_overlay_lib_uses_the_sealed_harness():
    offenders = {
        p.name: reason
        for p in _candidate_files()
        if (reason := _unsealed_reason(p.read_text(encoding="utf-8"))) is not None
    }
    assert offenders == {}, (
        f"these unit tests source scripts/lib/dev-test-overlays.sh with a hand-rolled "
        f"preamble: {offenders}. Sourcing it puts a real `docker` and an EXIT trap that runs "
        f"`docker stop` in reach of the LIVE dev stack — that is how the unit suite came to "
        f"SIGKILL opentranscribe-mock-llm on every run. Build the script with "
        f"{HARNESS_IMPORT}.{SEALED_BUILDER}()."
    )


def test_the_detector_finds_the_modules_that_actually_source_the_lib():
    """Guard the guard: a detector that matches nothing passes everything.

    The two modules below drive the real bash library; if the detector stops seeing them the
    test above becomes an empty loop that can never fail.
    """
    found = {p.name for p in _candidate_files()}
    for expected in (
        "test_dev_test_overlay_recreate_keeps_env.py",
        "test_dev_test_diar_native_overlay.py",
    ):
        assert expected in found, (
            f"{expected} sources the overlay library but the detector missed it "
            f"(found {sorted(found)}) — the seal check above is now vacuous"
        )


def test_the_detector_fires_on_an_unsealed_module():
    """Must-fire control, on synthetic source rather than a real file."""
    unsealed = (
        'script = f"""\n'
        "    REPO_ROOT={REPO_ROOT!s}\n"
        '    source "{OVERLAY_LIB!s}"\n'
        "    setup_overlays\n"
        '"""\n'
    )
    assert _runs_overlay_lib(unsealed), "detector missed a module that sources the library"
    assert _unsealed_reason(unsealed) == f"does not import {HARNESS_IMPORT}"


def test_the_detector_stays_clean_on_a_module_that_only_reads_the_lib():
    """Must-stay-clean control: reading the file to assert on its text is not sourcing it.

    ``test_compose_bringup_delegation.py`` and ``test_run_dev_tests_overlay_coverage.py`` both
    do exactly this, and neither needs the seal.
    """
    reader = (
        "OVERLAY_LIB = REPO_ROOT / 'scripts' / 'lib' / 'dev-test-overlays.sh'\n"
        "text = OVERLAY_LIB.read_text(encoding='utf-8')\n"
        "assert '[diar-native]=diar-native' in text\n"
    )
    assert not _runs_overlay_lib(reader), "detector fired on a module that only reads the lib"


def test_a_sealed_module_is_accepted():
    """Must-stay-clean control for the seal check itself."""
    sealed = (
        f"from {HARNESS_IMPORT} import {SEALED_BUILDER}\n"
        f'script = {SEALED_BUILDER}(tmp_path, root, """\n'
        '    source "{OVERLAY_LIB!s}"\n'
        '    setup_overlays\n"""'
        ")\n"
    )
    assert _runs_overlay_lib(sealed)
    assert _unsealed_reason(sealed) is None
