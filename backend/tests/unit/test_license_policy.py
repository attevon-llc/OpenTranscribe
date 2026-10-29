"""Characterization of the permissive-only licence policy against the committed reports."""

import csv
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = REPO_ROOT / "scripts" / "lib" / "license_policy.py"
REPORT_DIR = REPO_ROOT / "security-reports" / "license-compliance"


def _load_policy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("license_policy_under_test", POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = _load_policy()


def _committed_pairs() -> list[tuple[str, str]]:
    """Every distinct (license string, committed status) pair across the committed CSVs."""
    pairs: set[tuple[str, str]] = set()
    for path in sorted(REPORT_DIR.glob("*.csv")):
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                pairs.add((row["License"], row["Status"]))
    return sorted(pairs)


COMMITTED = _committed_pairs()


def _label(raw: str | None) -> str:
    category, _reason = policy.classify_license_string(raw)
    return str(policy.CATEGORY_LABEL[category])


def test_committed_reports_cover_every_status() -> None:
    assert len(COMMITTED) > 100
    assert {status for _lic, status in COMMITTED} >= {"OK", "REVIEW", "BLOCKER"}


@pytest.mark.parametrize(("license_string", "status"), COMMITTED)
def test_classification_matches_committed_report(license_string: str, status: str) -> None:
    assert _label(license_string) == status


def test_committed_license_strings_have_one_status() -> None:
    by_license: dict[str, set[str]] = {}
    for lic, status in COMMITTED:
        by_license.setdefault(lic, set()).add(status)
    assert {lic: s for lic, s in by_license.items() if len(s) > 1} == {}


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        ("MIT", "allow"),
        ("OFL-1.1", "allow"),
        ("LGPL-2.1-or-later", "concern"),
        ("MPL-2.0", "concern"),
        ("GPL-3.0-only", "blocker"),
        ("AGPL-3.0", "blocker"),
        ("SSPL-1.0", "blocker"),
        ("BSL-1.1", "blocker"),
        ("BSL-1.0", "allow"),
        ("LicenseRef-NVIDIA-Proprietary", "proprietary"),
        ("MIT OR GPL-3.0-only", "allow"),
        ("(MIT OR GPL-3.0-only)", "allow"),
        ("GPL-2.0-only OR LGPL-2.1-only", "concern"),
        ("GPL-2.0-only OR AGPL-3.0-only", "blocker"),
        ("MIT AND Apache-2.0", "allow"),
        ("MIT AND LGPL-2.1-only", "concern"),
        ("MIT AND GPL-2.0-only", "blocker"),
        ("MIT AND SomethingOdd", "unknown"),
        ("GPL-2.0-or-later WITH Bison-exception", "blocker"),
    ],
)
def test_expression_classification(raw: str, category: str) -> None:
    assert policy.classify_license_string(raw)[0] == category


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "UNKNOWN", "NOASSERTION", "garbage-license-xyz", "Other"]
)
def test_unrecognised_input_is_never_allowed(raw: str | None) -> None:
    category, reason = policy.classify_license_string(raw)
    assert category in {"unknown", "blocker"}
    assert reason


def test_policy_sets_do_not_overlap() -> None:
    allow, concern, blocker = policy.ALLOW, policy.CONCERN, policy.BLOCKER_EXACT
    assert not (allow & concern)
    assert not (allow & blocker)
    assert not (concern & blocker)
