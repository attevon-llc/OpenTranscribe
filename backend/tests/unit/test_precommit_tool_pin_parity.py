"""Pre-commit hooks that run a tool must pin the same version as ``requirements-dev.txt``.

A hook ``rev`` that lags the dev pin makes the commit gate and ``backend/venv`` disagree about
what is a finding (and what ``ruff format`` rewrites), so a tree that is green locally goes red
in the other place. Companion to ``test_precommit_mypy_env_parity.py``, which covers mypy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
REQUIREMENTS_DEV = REPO_ROOT / "backend" / "requirements-dev.txt"

RUFF_REPO = "https://github.com/astral-sh/ruff-pre-commit"


def _dev_pin(text: str, package: str) -> str:
    match = re.search(rf"^{re.escape(package)}==([^\s#;]+)", text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"{package}== pin not found in requirements-dev.txt")
    return match.group(1)


def _ruff_hook_rev(config: dict[str, Any]) -> str:
    for repo in config["repos"]:
        if repo.get("repo") == RUFF_REPO:
            return str(repo["rev"])
    raise AssertionError("ruff-pre-commit repo not found in .pre-commit-config.yaml")


def _import_linter_dependencies(config: dict[str, Any]) -> list[str]:
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == "import-linter":
                return [str(d) for d in hook.get("additional_dependencies", [])]
    raise AssertionError("import-linter hook not found in .pre-commit-config.yaml")


def _hook_dependencies(config: dict[str, Any], hook_id: str) -> list[str]:
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == hook_id:
                return [str(d) for d in hook.get("additional_dependencies", [])]
    raise AssertionError(f"{hook_id} hook not found in .pre-commit-config.yaml")


def _load() -> tuple[dict[str, Any], str]:
    return yaml.safe_load(HOOK_CONFIG.read_text()), REQUIREMENTS_DEV.read_text()


def test_ruff_hook_rev_equals_requirements_dev_pin() -> None:
    config, req = _load()
    assert _ruff_hook_rev(config) == f"v{_dev_pin(req, 'ruff')}"


def test_import_linter_hook_dependency_is_exact_dev_pin() -> None:
    config, req = _load()
    assert _import_linter_dependencies(config) == [
        f"import-linter=={_dev_pin(req, 'import-linter')}"
    ]


def test_ruff_ratchet_hook_uses_the_exact_dev_pin() -> None:
    # The ratchet guard runs ruff itself and refuses any other version; its hook env must
    # carry the pin the venv carries, or the guard passes locally and fails in CI.
    config, req = _load()
    assert _hook_dependencies(config, "guard-ruff-ratchet") == [f"ruff=={_dev_pin(req, 'ruff')}"]


def test_vulture_hook_uses_the_exact_dev_pin() -> None:
    # check_vulture.py refuses any other vulture version; its hook env must carry the dev pin.
    config, req = _load()
    assert _hook_dependencies(config, "guard-vulture") == [f"vulture=={_dev_pin(req, 'vulture')}"]


def test_checkers_detect_a_mismatch() -> None:
    config = {
        "repos": [
            {"repo": RUFF_REPO, "rev": "v0.1.0", "hooks": [{"id": "ruff"}]},
            {
                "repo": "local",
                "hooks": [
                    {"id": "import-linter", "additional_dependencies": ["import-linter>=2.0"]}
                ],
            },
        ]
    }
    assert _ruff_hook_rev(config) != "v0.16.7"
    assert _import_linter_dependencies(config) != ["import-linter==2.15"]
    assert _dev_pin("ruff==0.16.7  # x\n", "ruff") == "0.16.7"
    stale = {
        "repos": [
            {"repo": "local", "hooks": [{"id": "h", "additional_dependencies": ["ruff==0.1"]}]}
        ]
    }
    assert _hook_dependencies(stale, "h") != ["ruff==0.16.7"]
    with pytest.raises(AssertionError):
        _hook_dependencies(stale, "missing")
