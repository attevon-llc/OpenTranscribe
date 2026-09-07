"""A rehearsal scenario must label EVERY compose file it stages, not a hand-listed subset.

The 2026-09-07 rehearsal ran scenario A to 24/25 assertions and then reported:

    Scenario A's containers did not go away; the next scenario cannot start
    opentranscribe-diar-native-1   Up 2 minutes (healthy)

so `upgrade-from-previous` and `lite-mode` both exited 3 — **NOT MEASURED**. One container
cost two thirds of the rehearsal, and nothing about it was a release defect.

Mechanism: the three scenarios called ``cp_inject_labels`` on ``docker-compose.yml`` and
``docker-compose.prod.yml`` by name. The ``diar-native`` service lives in
``docker-compose.diar-native.yml``, so its container was created carrying the stock compose
project label but **not** the release-test label — and ``gr_cleanup`` removes containers by
that label alone. It survived teardown, kept ``opentranscribe_default`` alive, and blocked
the next scenario's port bind.

Two things made it invisible:

* ``diar-native`` is the only service in the tree with **no explicit ``container_name``**
  (hence compose's default ``-1`` suffix), so a name-shaped check would have missed it too.
* The scenario itself passed. Teardown is not an assertion, so nothing went red until the
  *next* scenario refused to start — which reads as a problem with that scenario.

The fix removes the enumeration rather than lengthening it: ``cp_inject_labels_all`` labels
every ``docker-compose*.yml`` in the staged directory, so a newly added overlay is covered
on arrival instead of the next time someone remembers.

These tests are static — no docker, no stack.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_TESTS = REPO_ROOT / "scripts" / "release-tests"
COMPOSE_PATCH = RELEASE_TESTS / "lib" / "compose-patch.sh"
SCENARIOS = [
    RELEASE_TESTS / "test-fresh-install.sh",
    RELEASE_TESTS / "test-upgrade.sh",
    RELEASE_TESTS / "test-lite-mode.sh",
]

pytestmark = pytest.mark.skipif(
    not COMPOSE_PATCH.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/lib/compose-patch.sh or bash is not present in this checkout",
)

# `cp_inject_labels "<path>" ...` — the per-FILE form, which is what has to disappear from
# the scenarios. `cp_inject_labels_all "<dir>"` must not match, hence the quote right after
# the function name.
_PER_FILE_CALL = re.compile(r'\bcp_inject_labels\s+"')


def _uncommented(path: Path) -> list[tuple[int, str]]:
    return [
        (n, line)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def test_no_scenario_labels_compose_files_one_by_one():
    offenders: list[str] = []
    for scenario in SCENARIOS:
        if not scenario.is_file():
            continue
        for n, line in _uncommented(scenario):
            if _PER_FILE_CALL.search(line):
                offenders.append(f"{scenario.relative_to(REPO_ROOT)}:{n}: {line.strip()}")

    assert not offenders, (
        "these scenarios label compose files by NAME. Any overlay not in the list creates "
        "containers without the release-test label, which gr_cleanup then cannot remove — "
        "that is how one surviving diar-native container made two of three rehearsal "
        "scenarios NOT MEASURED. Use cp_inject_labels_all <dir>:\n  " + "\n  ".join(offenders)
    )


def test_every_scenario_actually_calls_the_all_form():
    """Non-vacuity: "no per-file calls" is also satisfied by labelling nothing at all."""
    missing = [
        str(scenario.relative_to(REPO_ROOT))
        for scenario in SCENARIOS
        if scenario.is_file()
        and not any("cp_inject_labels_all" in line for _, line in _uncommented(scenario))
    ]
    assert not missing, (
        f"these scenarios label no compose files at all: {missing}. Every container they "
        "create would then survive gr_cleanup."
    )


def test_the_diar_native_overlay_is_the_case_this_protects():
    """Prove the premise instead of asserting it.

    If diar-native ever gains a container_name, or moves into the base compose file, the
    story in this module's docstring stops being true and should be re-checked rather than
    left passing for free.
    """
    overlay = REPO_ROOT / "docker-compose.diar-native.yml"
    if not overlay.is_file():
        pytest.skip("docker-compose.diar-native.yml is not in this checkout")

    text = overlay.read_text(encoding="utf-8")
    assert "diar-native:" in text, (
        "the diar-native service is no longer defined in docker-compose.diar-native.yml — "
        "this module's premise needs revisiting"
    )
    assert "container_name" not in text, (
        "docker-native now declares a container_name. That does not make the labelling bug "
        "safe (gr_cleanup matches on the LABEL, not the name), but it does mean the "
        "compose-default `-1` name in this module's docstring is stale."
    )


# --------------------------------------------------------- the helper does what it claims


def test_cp_inject_labels_all_labels_an_overlay_the_old_code_missed(tmp_path: Path):
    """Drive the REAL function over a staged tree shaped like the one that broke.

    Static checks above prove the scenarios *call* it; this proves the call does the work —
    specifically that the third file, the one no scenario named, comes back labelled.
    """
    if shutil.which("python3") is None:  # pragma: no cover - python3 runs this test
        pytest.skip("python3 unavailable")

    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "docker-compose.yml").write_text(
        "services:\n  backend:\n    image: busybox\n", encoding="utf-8"
    )
    (stage / "docker-compose.prod.yml").write_text(
        "services:\n  backend:\n    image: busybox\n", encoding="utf-8"
    )
    # The file the old, name-enumerating code never touched.
    (stage / "docker-compose.diar-native.yml").write_text(
        "services:\n  diar-native:\n    image: busybox\n", encoding="utf-8"
    )
    # A non-compose YAML in the same directory must be left alone.
    (stage / "unrelated.yml").write_text(
        "services:\n  nope:\n    image: busybox\n", encoding="utf-8"
    )

    label = "com.opentranscribe.release-test=selftest"
    harness = textwrap.dedent(f"""
        set -euo pipefail
        source "{COMPOSE_PATCH}"
        cp_inject_labels_all "{stage}" "{label}"
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, f"cp_inject_labels_all failed:\n{result.stderr}"

    overlay_text = (stage / "docker-compose.diar-native.yml").read_text(encoding="utf-8")
    assert "com.opentranscribe.release-test" in overlay_text, (
        "the diar-native overlay came back UNLABELLED — the exact state whose container "
        f"survived teardown:\n{overlay_text}"
    )
    for named in ("docker-compose.yml", "docker-compose.prod.yml"):
        assert "com.opentranscribe.release-test" in (stage / named).read_text(encoding="utf-8"), (
            f"{named} lost its label — the fix must be a superset of what it replaced"
        )
    assert "com.opentranscribe.release-test" not in (stage / "unrelated.yml").read_text(
        encoding="utf-8"
    ), "a non-compose YAML beside the stack was rewritten; the glob is too wide"


def test_cp_inject_labels_all_refuses_an_empty_directory(tmp_path: Path):
    """Labelling nothing must be an error, not a silent success.

    A staged tree with no compose files means the staging step failed; returning 0 there
    would hand the caller a stack whose containers are all unremovable, reported as fine.
    """
    harness = textwrap.dedent(f"""
        set -uo pipefail
        source "{COMPOSE_PATCH}"
        if cp_inject_labels_all "{tmp_path}" "k=v"; then echo "RC=0"; else echo "RC=$?"; fi
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False
    )
    assert "RC=0" not in result.stdout, (
        "cp_inject_labels_all reported success over a directory containing no compose files"
    )
