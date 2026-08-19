"""Every ``integration``-marked test must live where the pre-merge gate looks for it.

``scripts/run-integration-tests.sh`` runs the integration phase over an explicit path list
rather than all of ``tests/``: there are 20 such tests and collecting the full 5,200-test tree
to find them cost ~23 s per phase. That is a worthwhile saving, but it introduces a way to be
silently wrong — an ``integration``-marked test added anywhere else would simply never run,
and the gate would still report success.

This is the same failure shape as ``gpu`` being unregistered before #297: the marker existed,
nothing selected it, and nobody noticed. So the narrowing is paired with this check rather
than trusted (issue #431).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_GATE_SCRIPT = _TESTS_ROOT.parents[1] / "scripts" / "run-integration-tests.sh"

#: Marker whose selection the gate narrows. ``gpu`` deliberately keeps the full-tree sweep —
#: it is cheap relative to loading CUDA models and those tests are scattered.
_MARKER = "integration"


def _marked_modules() -> set[str]:
    """Modules containing at least one ``integration``-marked test or module-level mark."""
    found: set[str] = set()
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if "e2e" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != _MARKER:
                continue
            # `pytest.mark.integration` — confirm it is a mark, not an attribute named the same.
            if isinstance(node.value, ast.Attribute) and node.value.attr == "mark":
                found.add(path.relative_to(_TESTS_ROOT).as_posix())
                break
    return found


def _gate_integration_paths() -> list[str]:
    """The paths the gate's integration phase actually passes to pytest.

    Matches ``run_phase`` **or** ``run_phase_watching_skips``: the integration
    phase moved to the latter so a mass-skipped run cannot report as a pass (see
    ``test_integration_gate_skip_ceiling.py``), and a pattern naming only the
    original dispatcher went red for a rename rather than for a real gap. The
    phase TITLE is the stable identity here, not the function that runs it.
    """
    source = _GATE_SCRIPT.read_text()
    match = re.search(r'run_phase\w* "Integration-marked tests".*?\n\n', source, re.S)
    assert match, "could not find the integration phase in run-integration-tests.sh"
    return re.findall(r"tests/[\w./-]+", match.group(0))


def test_the_gate_script_still_has_an_integration_phase() -> None:
    """If the phase is renamed or removed, this module's premise is gone — fail loudly."""
    assert _gate_integration_paths(), (
        "the integration phase in run-integration-tests.sh passes no tests/ path; "
        "either restore it or delete this guard"
    )


def test_every_integration_marked_test_is_reachable_by_the_gate() -> None:
    """A marked test outside the gate's paths never runs, and the gate still reports green."""
    gate_paths = _gate_integration_paths()
    unreachable = []
    for module in sorted(_marked_modules()):
        candidate = f"tests/{module}"
        if not any(candidate == p or candidate.startswith(p.rstrip("/") + "/") for p in gate_paths):
            unreachable.append(candidate)

    assert not unreachable, (
        "These modules carry @pytest.mark.integration but sit outside the paths "
        f"run-integration-tests.sh collects ({gate_paths}), so the gate would skip them "
        "silently:\n  " + "\n  ".join(unreachable) + "\nAdd the path to the gate script, or "
        "move the test under tests/integration/."
    )
