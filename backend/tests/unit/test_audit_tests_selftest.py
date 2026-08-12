"""Run ``scripts/audit-tests.py --selftest`` as part of the ordinary test suite.

The auditor's detectors are code, and code stops matching. A detector that matches nothing
reports zero findings, which is **indistinguishable from a clean suite** — the exact failure
mode the auditor exists to catch, turned on itself. Its frontend sibling's self-test caught
two dead detectors that way, so the flag exists; this file makes sure it runs even when
nobody remembers to type it.

Cheap by construction: every case is a string parsed in memory. No filesystem walk, no DB,
no subprocess — the whole file is a few milliseconds, so it belongs in the fast suite rather
than behind a marker.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_AUDITOR_PATH = Path(__file__).resolve().parents[3] / "scripts" / "audit-tests.py"


def _load_auditor() -> ModuleType:
    """Import ``scripts/audit-tests.py``, whose hyphenated name blocks a normal import."""
    spec = importlib.util.spec_from_file_location("audit_tests", _AUDITOR_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {_AUDITOR_PATH}"
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module: `@dataclass` on `Finding` resolves its `from __future__
    # import annotations` string types through `sys.modules[cls.__module__]`, which is None
    # for a module that is not registered yet — an AttributeError deep inside dataclasses.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


auditor = _load_auditor()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("category", "source"),
    auditor.SELFTEST_CASES,
    ids=[f"{i}-{c}" for i, (c, _) in enumerate(auditor.SELFTEST_CASES)],
)
def test_detector_fires(category: str, source: str) -> None:
    """Each must-fire fixture produces its category. A silent detector is a dead detector."""
    fired = {f.category for f in auditor.scan_source(source, "fixture.py")}
    assert category in fired, (
        f"detector `{category}` did not fire on its own fixture; got {sorted(fired) or 'nothing'}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    auditor.SELFTEST_CLEAN,
    ids=[f"clean-{i}" for i in range(len(auditor.SELFTEST_CLEAN))],
)
def test_clean_fixture_produces_no_finding(source: str) -> None:
    """The false-positive half. Each of these encodes a calibration bug already made once."""
    findings = auditor.scan_source(source, "fixture.py")
    assert not findings, "; ".join(f"{f.category}: {f.detail}" for f in findings)


@pytest.mark.unit
def test_every_category_has_a_must_fire_case() -> None:
    """A detector with no fixture is a detector nobody would notice going blind."""
    covered = {category for category, _ in auditor.SELFTEST_CASES}
    missing = sorted(set(auditor.CATEGORIES) - covered)
    assert not missing, f"detectors with no --selftest case: {missing}"


@pytest.mark.unit
def test_allowlist_reasons_are_present_and_categorised() -> None:
    """Every allowlist entry needs three key segments and a non-empty reason.

    The category being part of the key is what stops one exemption covering every detector;
    an entry that lost it would silently widen to all fourteen.
    """
    root = Path(__file__).resolve().parents[1]
    allowed = auditor.load_allowlist(root)
    assert allowed, "the allowlist should not be empty — did the path move?"
    bad_keys = sorted(k for k in allowed if len(k.split("::")) != 3)
    unknown = sorted(k for k in allowed if k.split("::")[-1] not in auditor.CATEGORIES)
    missing_reason = sorted(k for k, r in allowed.items() if r == "no reason given")
    assert not bad_keys, f"keys must be <file>::<test>::<category>: {bad_keys}"
    assert not unknown, f"keys naming a detector that does not exist: {unknown}"
    assert not missing_reason, f"entries with no written reason: {missing_reason}"
