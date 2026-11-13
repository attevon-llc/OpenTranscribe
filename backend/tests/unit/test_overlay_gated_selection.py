"""``run-dev-tests.sh --overlay-only`` selects tests by the ``overlay_gated`` marker.

The pre-release matrix leg ``2a-overlays`` used to re-run the whole backend gate only so the tests
that need an overlay container (watch sources, mock ASR) would execute. It now runs just the
marked tests, which is only correct while the marker is complete: an overlay-dependent test
without it would silently never run before a release. Marker selection is pytest's documented
mechanism (https://docs.pytest.org/en/stable/how-to/mark.html); this module is the completeness
check, derived from the tests themselves rather than a hand list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_REPO = _BACKEND.parent
_TESTS = _BACKEND / "tests"

#: Names that make a test module overlay-dependent: the `watch` marker (needs --with-watch) and the
#: mock-ASR port its skipif probes (needs --with-mock-asr). Both are the mechanisms by which
#: today's overlay-gated modules skip without their container.
_OVERLAY_MARKS = {"watch"}
_OVERLAY_NAMES = {"MOCK_ASR_PORT"}


def _marks_in(tree: ast.AST) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    }


def _names_in(tree: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def modules_missing_marker(sources: dict[str, str]) -> list[str]:
    """Overlay-dependent modules (by the signals above) that lack ``overlay_gated``."""
    missing = []
    for name, source in sorted(sources.items()):
        tree = ast.parse(source)
        dependent = bool(_marks_in(tree) & _OVERLAY_MARKS) or bool(_names_in(tree) & _OVERLAY_NAMES)
        if dependent and "overlay_gated" not in _marks_in(tree):
            missing.append(name)
    return missing


def _repo_sources() -> dict[str, str]:
    skip = {Path(__file__).resolve()}
    return {
        str(p.relative_to(_TESTS)): p.read_text(encoding="utf-8")
        for p in sorted(_TESTS.rglob("test_*.py"))
        if p.resolve() not in skip
    }


def test_detector_fires_on_an_unmarked_overlay_module() -> None:
    watch = "import pytest\npytestmark = pytest.mark.watch\n"
    mock_asr = "MOCK_ASR_PORT = 1\ndef test_x():\n    assert MOCK_ASR_PORT\n"
    assert modules_missing_marker({"a.py": watch, "b.py": mock_asr}) == ["a.py", "b.py"]


def test_detector_stays_clean_on_marked_and_unrelated_modules() -> None:
    marked = "import pytest\npytestmark = [pytest.mark.watch, pytest.mark.overlay_gated]\n"
    unrelated = "import pytest\n@pytest.mark.slow\ndef test_x():\n    pass\n"
    assert modules_missing_marker({"a.py": marked, "b.py": unrelated}) == []


def test_every_overlay_dependent_test_module_carries_the_marker() -> None:
    assert modules_missing_marker(_repo_sources()) == []


def test_the_selection_is_not_empty_in_both_pytest_roots() -> None:
    marked = [n for n, s in _repo_sources().items() if "overlay_gated" in _marks_in(ast.parse(s))]
    assert any(n.startswith("integration/") for n in marked), "no marked backend integration test"
    assert any(n.startswith("e2e/") for n in marked), "no marked e2e test"


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        (
            "scripts/run-integration-tests.sh",
            'INTEGRATION_SELECTION="integration and overlay_gated"',
        ),
        ("scripts/run-dev-tests.sh", "-m overlay_gated"),
        ("scripts/test-matrix.sh", "scripts/run-dev-tests.sh --overlay-only --search-quality"),
    ],
)
def test_the_scripts_select_by_the_marker(path: str, needle: str) -> None:
    assert needle in (_REPO / path).read_text(encoding="utf-8")


def _run_e2e_skip_check(tmp_path: Path, skipped: int) -> int:
    """Run run-e2e.sh's own ``skips_exceed_ceiling`` (extracted verbatim) on a junit report."""
    import re
    import subprocess
    import sys

    src = (_REPO / "scripts/e2e/run-e2e.sh").read_text(encoding="utf-8")
    funcs = []
    for name in ("phase_skipped", "skips_exceed_ceiling"):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", src, re.S | re.M)
        assert match, f"{name} missing from run-e2e.sh"
        funcs.append(match.group(0))
    xml = tmp_path / "r.xml"
    xml.write_text(
        f'<testsuites><testsuite tests="1" skipped="{skipped}"/></testsuites>', encoding="utf-8"
    )
    script = (
        f'VENV_PY="{sys.executable}"\n' + "\n".join(funcs) + f'\nskips_exceed_ceiling "{xml}" 0\n'
    )
    return subprocess.run(["bash", "-c", script], check=False).returncode


def test_an_overlay_only_e2e_skip_fails_the_phase(tmp_path: Path) -> None:
    assert _run_e2e_skip_check(tmp_path, skipped=1) == 0, "a skipped test must exceed ceiling 0"


def test_an_overlay_only_e2e_run_without_skips_passes(tmp_path: Path) -> None:
    assert _run_e2e_skip_check(tmp_path, skipped=0) != 0


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("scripts/run-dev-tests.sh", "E2E_CUSTOM_SKIP_CEILING=0"),
        ("scripts/e2e/run-e2e.sh", 'exit "$EXIT_NOT_MEASURED"'),
        ("scripts/run-integration-tests.sh", "    INTEGRATION_SKIP_CEILING=0\n"),
    ],
)
def test_overlay_only_pins_the_skip_ceiling_to_zero(path: str, needle: str) -> None:
    assert needle in (_REPO / path).read_text(encoding="utf-8")
