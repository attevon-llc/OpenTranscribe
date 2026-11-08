"""The third-party licence page must name every non-permissive package in the generated inventory.

``pinned-requirements-licenses.csv`` is written by ``scripts/check-licenses.py``; the page is
prose, so the only thing a tool can check is coverage. A package family may be covered by a
backticked wildcard such as ``lightningcss*``.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INVENTORY = ROOT / "security-reports" / "license-compliance" / "pinned-requirements-licenses.csv"
DOC = ROOT / "docs-site" / "docs" / "legal" / "third-party-licenses.md"
NOTICE = ROOT / "NOTICE"
COVERED_CLASSES = {"REVIEW", "BLOCKER", "PROPRIETARY-REVIEW", "UNKNOWN"}
REQUIRED = ("mutagen", "fpdf2", "ldap3", "psycopg2", "certifi", "tqdm", "lightningcss", "Silo")


def _documented_tokens() -> set[str]:
    return set(re.findall(r"`([^`\s]+)`", DOC.read_text(encoding="utf-8")))


def _covered(name: str, tokens: set[str]) -> bool:
    if name in tokens:
        return True
    return any(t.endswith("*") and name.startswith(t[:-1]) for t in tokens)


def _flagged() -> list[str]:
    with INVENTORY.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return sorted({r["package"] for r in rows if r["class"] in COVERED_CLASSES})


def test_inventory_has_flagged_packages() -> None:
    assert len(_flagged()) >= 8, "inventory unreadable or the classes changed; the check is vacuous"


@pytest.mark.parametrize("name", _flagged())
def test_flagged_package_is_documented(name: str) -> None:
    assert _covered(name, _documented_tokens()), (
        f"{name} is non-permissive in the licence inventory but not named in {DOC.name}"
    )


@pytest.mark.parametrize("name", REQUIRED)
def test_required_components_named(name: str) -> None:
    assert name in DOC.read_text(encoding="utf-8")


def test_notice_links_the_page() -> None:
    assert "third-party-licenses" in NOTICE.read_text(encoding="utf-8")
