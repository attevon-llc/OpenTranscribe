"""The error-disclosure gate must be able to fail (issue #959).

``scripts/audit-error-disclosure.py`` turns "every read edge of a stored failure column is
sanitized" into an enforced property. A detector that silently stops matching reports zero
findings, which reads exactly like a clean tree, so its self-test runs here too; and the real
tree is scanned so a new un-sanitized edge fails the suite, not just the pre-commit hook.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "audit-error-disclosure.py"


@pytest.fixture(scope="module")
def auditor():
    if not _SCRIPT.exists():
        pytest.fail(f"{_SCRIPT} is missing — the error-disclosure gate has no implementation")
    spec = importlib.util.spec_from_file_location("audit_error_disclosure", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_error_disclosure"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_every_detector_is_alive(auditor):
    assert auditor.run_selftest(verbose=False) == []


@pytest.mark.unit
def test_the_app_tree_has_no_findings(auditor):
    root = _REPO / "backend" / "app"
    findings = [f for p in sorted(root.rglob("*.py")) for f in auditor.scan_file(p, root)]
    assert findings == []


@pytest.mark.unit
def test_an_unsanitized_task_row_on_the_wire_is_caught(auditor):
    """The exact shape #786 missed in ``user_files.py``."""
    source = (
        "def get_file_detailed_status(tasks):\n"
        '    return [{"error_message": task.error_message} for task in tasks]\n'
    )
    found = auditor.scan_source(source, "api/endpoints/user_files.py")
    assert [(f.category, f.line) for f in found] == [("raw-error-read", 2)]


@pytest.mark.unit
def test_retry_rederived_from_stored_prose_is_caught(auditor):
    source = (
        "def recover(media_file):\n"
        '    return categorize_error(media_file.last_error_message or "")\n'
    )
    found = auditor.scan_source(source, "services/task_recovery_service.py")
    assert [f.category for f in found] == ["prose-retry-rederivation"]
