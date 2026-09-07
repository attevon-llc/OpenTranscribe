"""Every file in the gate's ``GATED_FILES`` must actually be gated.

``scripts/run-integration-tests.sh`` runs ``GATED_FILES`` twice — once with the ``RUN_*``
variables set and FIPS off, once with them set and ``FIPS_MODE=true``. A file in that list
which carries **no** ``RUN_*`` gate is not gated by anything, so it also runs in the ungated
Unit/API phase: three executions of the same tests per gate run.

``tests/test_admin_endpoints.py`` sat there with no gate (``rg RUN_`` found nothing in it), so
its 8 tests ran 24 times per gate and the gated phases' pass counts overstated what those
variables actually unlock. That is not merely wasteful — the whole point of the two gated
passes is to say "these suites behave the same in both FIPS modes", and a file that is not
FIPS-aware and not gated contributes a number to that claim while testing nothing about it.

Static, and about a shell script, so it belongs in the fast unit suite: the failure is invisible
at runtime (three green runs look like three green runs).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"
BACKEND = REPO_ROOT / "backend"

pytestmark = pytest.mark.skipif(
    not GATE.is_file(), reason="scripts/run-integration-tests.sh not in this checkout"
)

#: A module-level env gate, in either spelling the tree uses.
_GATE_PATTERN = re.compile(r"RUN_[A-Z0-9_]+")


def _gated_files() -> list[str]:
    source = GATE.read_text(encoding="utf-8")
    block = re.search(r"^GATED_FILES=\((.*?)\)$", source, re.S | re.M)
    assert block, "GATED_FILES array not found in run-integration-tests.sh"
    return re.findall(r"tests/[\w./-]+\.py", block.group(1))


def _declared_gates() -> set[str]:
    """The RUN_* variables the gate script exports for that phase."""
    source = GATE.read_text(encoding="utf-8")
    block = re.search(r"^GATES=\((.*?)\)$", source, re.S | re.M)
    assert block, "GATES array not found in run-integration-tests.sh"
    return set(_GATE_PATTERN.findall(block.group(1)))


def test_the_lists_are_non_empty():
    """Non-vacuity: everything below iterates these, so an empty parse passes everything."""
    assert _gated_files(), "GATED_FILES parsed as empty — the check below would be an empty loop"
    assert _declared_gates(), "GATES parsed as empty"


def test_every_gated_file_reads_one_of_the_declared_gates():
    gates = _declared_gates()
    ungated = []
    for rel in _gated_files():
        path = BACKEND / rel
        if not path.is_file():
            ungated.append(f"{rel} (file does not exist)")
            continue
        text = path.read_text(encoding="utf-8")
        if not any(gate in text for gate in gates):
            ungated.append(rel)

    assert not ungated, (
        f"these files are in GATED_FILES but read none of {sorted(gates)}: {ungated}. "
        f"An ungated file there runs THREE times per gate — once in the Unit/API phase and "
        f"once in each FIPS pass — and inflates the gated phases' counts with tests that say "
        f"nothing about what those variables unlock. Either add its gate, or drop it from "
        f"GATED_FILES and let the Unit/API phase cover it."
    )


def test_a_file_with_no_gate_would_be_detected(tmp_path: Path):
    """Guard the guard, with the exact shape that was in the list.

    The detector is "does the file mention any declared RUN_* variable", so a must-fire case
    has to be a file that mentions none. Without this, a parse that silently returned an empty
    gate set would make the check above pass over anything.
    """
    gates = _declared_gates()
    ungated_source = (
        "def test_admin_can_list_users(client):\n    assert client.get('/x').status_code == 200\n"
    )
    assert not any(gate in ungated_source for gate in gates)

    gated_source = f"pytestmark = pytest.mark.skipif(not os.environ.get('{sorted(gates)[0]}'))\n"
    assert any(gate in gated_source for gate in gates)
