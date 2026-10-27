"""The offline half of scripts/check-requirements-resolve.py.

The script's real job (``pip install --dry-run`` against PyPI) needs the network and runs in
CI. What can be proven offline is the logic that decides pass/fail from a pip report: which
packages count as drift, and that a local tag or a documented exemption does not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-requirements-resolve.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_requirements_resolve", SCRIPT)
    assert spec and spec.loader, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def _report(**versions: str) -> dict[str, object]:
    return {"install": [{"metadata": {"name": n, "version": v}} for n, v in versions.items()]}


def test_resolved_versions_normalises_names() -> None:
    report = _report(**{"pyannote.audio": "4.0.7", "Typing_Extensions": "4.15.0"})
    assert check.resolved_versions(report) == {
        "pyannote-audio": "4.0.7",
        "typing-extensions": "4.15.0",
    }


def test_resolved_versions_rejects_a_report_with_no_install_list() -> None:
    with pytest.raises(ValueError, match="install"):
        check.resolved_versions({"version": "1"})


def test_a_different_version_is_drift() -> None:
    drift = check.find_drift({"starlette": "1.9.0"}, {"starlette": "1.10.0"})
    assert drift == ["starlette: prod=1.9.0 variant=1.10.0"]


def test_a_local_tag_is_not_drift() -> None:
    assert check.find_drift({"torch": "2.11.0+cu128"}, {"torch": "2.11.0+cpu"}) == []


def test_a_package_only_one_side_resolves_is_not_drift() -> None:
    assert check.find_drift({"onnxruntime-gpu": "1.30.0"}, {"onnxruntime": "1.30.0"}) == []


def test_documented_exemptions_are_honoured_and_others_are_not() -> None:
    reference = {"pyannote-audio": "4.0.5.dev18+gabc", "anyio": "4.15.1"}
    variant = {"pyannote-audio": "4.0.7", "anyio": "4.16.0"}
    assert check.find_drift(reference, variant) == ["anyio: prod=4.15.1 variant=4.16.0"]


def test_every_exemption_carries_a_reason() -> None:
    assert check.PARITY_EXEMPT
    assert all(len(reason) > 20 for reason in check.PARITY_EXEMPT.values())


def test_requirements_txt_is_resolved_first_because_it_is_the_reference() -> None:
    assert check.STEPS[0][0] == "requirements.txt"
    assert ("requirements-nodeps.txt", ("--no-deps",)) in check.STEPS


def test_selecting_a_variant_also_resolves_the_reference_it_is_compared_with() -> None:
    names = [name for name, _ in check.select_steps(["requirements-lite.txt"])]
    assert names == ["requirements.txt", "requirements-lite.txt"]


def test_a_selection_without_a_parity_file_is_taken_as_given() -> None:
    names = [name for name, _ in check.select_steps(["requirements-nodeps.txt"])]
    assert names == ["requirements-nodeps.txt"]


def test_no_selection_runs_every_step_in_order() -> None:
    assert check.select_steps(None) == list(check.STEPS)
