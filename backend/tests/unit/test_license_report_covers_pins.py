"""The committed pinned-requirements licence inventory must cover every pin the product ships.

``security-reports/license-compliance/pinned-requirements-licenses.csv`` is written by
``scripts/check-licenses.py`` (its ``license-gate.csv`` evidence) from the resolved closure of the
shipped requirements files plus the frontend lockfile. The image-level trivy report beside it
describes a past release; this file is what a reviewer can check against the current pins.

A pin that is missing from the inventory means the report was generated from different inputs than
the ones that ship, so the report would read as complete while omitting a package.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "security-reports" / "license-compliance" / "pinned-requirements-licenses.csv"
SHIPPED = ("requirements.txt", "requirements-nodeps.txt", "requirements-lite.txt")
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")

#: Packages the owner asked to see named, with the class the policy must give them.
NAMED = {"fpdf2": "REVIEW", "python-docx": "OK", "uharfbuzz": "OK"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _public(version: str) -> str:
    """Drop the PEP 440 local tag (``+cpu``/``+cu128``): the index variant, not the release."""
    return version.split("+", 1)[0]


def _rows() -> list[dict[str, str]]:
    with REPORT.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _pins() -> set[tuple[str, str]]:
    pins: set[tuple[str, str]] = set()
    for name in SHIPPED:
        for line in (ROOT / "backend" / name).read_text(encoding="utf-8").splitlines():
            m = PIN.match(line.strip())
            if m:
                pins.add((_norm(m.group(1)), _public(m.group(2))))
    return pins


def test_every_pinned_python_requirement_is_in_the_report() -> None:
    pins = _pins()
    assert len(pins) > 50, "pin parser found almost nothing; the test would pass vacuously"
    reported = {
        (_norm(r["package"]), _public(r["version"])) for r in _rows() if r["ecosystem"] == "pip"
    }
    assert sorted(pins - reported) == []


def test_every_production_npm_package_is_in_the_report() -> None:
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    prod = {
        (path.rsplit("node_modules/", 1)[1], meta["version"])
        for path, meta in lock["packages"].items()
        if "node_modules/" in path and not meta.get("dev") and "version" in meta
    }
    assert len(prod) > 20, "lockfile parse found almost nothing; the test would pass vacuously"
    reported = {(r["package"], r["version"]) for r in _rows() if r["ecosystem"] == "npm"}
    assert sorted(prod - reported) == []


@pytest.mark.parametrize(("package", "expected"), sorted(NAMED.items()))
def test_named_packages_are_listed_with_their_classification(package: str, expected: str) -> None:
    rows = [r for r in _rows() if r["ecosystem"] == "pip" and _norm(r["package"]) == package]
    assert rows, f"{package} missing from the report"
    assert {r["class"] for r in rows} == {expected}
