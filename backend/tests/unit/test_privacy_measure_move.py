"""``Residual``, ``GroupedCounts`` and ``merge_grouped`` have exactly one home (issue #1353, R4).

The privacy purge and the erasure receipt measure the same two things; a second definition of
either is how their "unmeasurable is not zero" rules drift apart.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.privacy import measure
from app.services.privacy import purge
from app.services.privacy import purge_residual

APP = Path(__file__).resolve().parents[2] / "app"
NAMES = {"Residual", "GroupedCounts", "merge_grouped"}


def _definitions() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {n: [] for n in NAMES}
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            for name in names:
                if name in NAMES:
                    found[name].append(str(path.relative_to(APP)))
    return found


def test_each_measurement_type_is_defined_exactly_once():
    found = _definitions()
    assert {k: v for k, v in found.items()} == {k: ["services/privacy/measure.py"] for k in NAMES}


def test_the_purge_modules_use_the_shared_types():
    assert purge.Residual is measure.Residual
    assert purge_residual.Residual is measure.Residual
    assert purge.GroupedCounts is measure.GroupedCounts


def test_the_residual_rule_is_unchanged():
    clean = measure.Residual(counts={"a": 0})
    assert clean.ok
    assert not measure.Residual(counts={"a": 1}).ok
    # Unmeasurable is not clean, even with every measured count at 0.
    assert not measure.Residual(counts={"a": 0}, unverified=["b"]).ok


def test_merge_grouped_adds_per_store_and_key():
    total = {"postgres": {"x": 1}}
    out = measure.merge_grouped(total, {"postgres": {"x": 2, "y": 1}, "s3": {"z": 4}})
    assert out == {"postgres": {"x": 3, "y": 1}, "s3": {"z": 4}}
