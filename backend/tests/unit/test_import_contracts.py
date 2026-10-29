"""The import-linter layer contracts in ``backend/pyproject.toml`` hold, and can fail.

A contract that cannot break is worse than none, so besides asserting the real tree is
clean, these tests copy ``app/`` to a throwaway tree, plant a violation (or fix an
ignored edge), and require the *specific* contract to break.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]

C_CLOUD = "Core (app) must not import the closed cloud edition"
C_API = "Lower layers must not import app.api"
C_TASKS = "Foundation layers must not import app.tasks"
C_LAYERS = "Application layers import downward only"
C_MODELS = "app.models must not import services, tasks or api"
C_UTILS = "app.utils must not import services, tasks or api"
ALL_CONTRACTS = [C_CLOUD, C_API, C_TASKS, C_LAYERS, C_MODELS, C_UTILS]


def _lint_imports_binary() -> str:
    beside_python = Path(sys.executable).parent / "lint-imports"
    if beside_python.exists():
        return str(beside_python)
    found = shutil.which("lint-imports")
    if found is None:
        if os.environ.get("CI"):
            pytest.fail("lint-imports is missing in CI; add import-linter to requirements-ci.txt")
        pytest.skip("lint-imports is not installed (pip install -r requirements-dev.txt)")
    return found


def _run(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_lint_imports_binary(), "--config", "pyproject.toml", "--no-cache"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _status(output: str) -> dict[str, str]:
    """Map contract name -> KEPT/BROKEN from the summary lines."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^(.+?) (KEPT|BROKEN)\b", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


@contextmanager
def _backend_copy() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="ot-redcheck."))
    try:
        shutil.copytree(BACKEND / "app", root / "app", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(BACKEND / "pyproject.toml", root / "pyproject.toml")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_real_tree_keeps_every_contract() -> None:
    result = _run(BACKEND)
    output = result.stdout + result.stderr
    status = _status(output)
    assert status == dict.fromkeys(ALL_CONTRACTS, "KEPT"), output
    assert result.returncode == 0, output


PLANTS = [
    pytest.param(
        "app/utils/_probe.py",
        "def f():\n    from app.api.deps_context import RequestContext\n    return RequestContext\n",
        "app/api/deps_context.py",
        {C_API, C_UTILS, C_LAYERS},
        id="utils-to-api",
    ),
    pytest.param(
        "app/db/_probe.py",
        "def f():\n    from app.tasks.thumbnail import generate_thumbnail_task\n"
        "    return generate_thumbnail_task\n",
        "app/tasks/thumbnail.py",
        {C_TASKS, C_LAYERS},
        id="db-to-tasks",
    ),
    pytest.param(
        "app/models/_probe.py",
        "def f():\n    from app.services.minio_service import upload_file\n    return upload_file\n",
        "app/services/minio_service.py",
        {C_MODELS, C_LAYERS},
        id="models-to-services",
    ),
    pytest.param(
        # Only the forbidden contract reaches app.middleware, and only with indirect
        # imports disallowed: app.main reaches app.api through the whole app.
        "app/middleware/_probe.py",
        "def f():\n    from app.main import app\n    return app\n",
        "app/main.py",
        {C_API},
        id="middleware-to-main-indirect",
    ),
    pytest.param(
        "app/services/_probe.py",
        "import stripe\n",
        None,
        {C_CLOUD},
        id="core-to-stripe",
    ),
]


@pytest.mark.parametrize(("plant_path", "plant_source", "target", "must_break"), PLANTS)
def test_planted_import_breaks_the_right_contracts(
    plant_path: str, plant_source: str, target: str | None, must_break: set[str]
) -> None:
    with _backend_copy() as root:
        clean = _run(root)
        assert clean.returncode == 0, clean.stdout + clean.stderr
        # grimp silently drops an import of a module that does not exist, so a plant
        # naming a missing target would prove nothing.
        if target is not None:
            assert (root / target).is_file(), f"plant target {target} does not exist"
        (root / plant_path).write_text(plant_source)
        result = _run(root)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    broken = {name for name, state in _status(output).items() if state == "BROKEN"}
    assert broken == must_break, output


def _contracts() -> dict[str, dict]:
    config = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    return {c["name"]: c for c in config["tool"]["importlinter"]["contracts"]}


def test_api_contract_covers_every_lower_package() -> None:
    # Shrinking the source set silently un-guards a package: a package with no ignores
    # has nothing to go stale.
    contract = _contracts()[C_API]
    assert set(contract["source_modules"]) == {
        "app.models",
        "app.schemas",
        "app.db",
        "app.core",
        "app.utils",
        "app.services",
        "app.tasks",
        "app.transcription",
        "app.auth",
        "app.middleware",
    }
    assert contract["forbidden_modules"] == ["app.api"]


def test_direct_only_contracts_disallow_indirect_imports() -> None:
    contracts = _contracts()
    for name in (C_API, C_MODELS):
        assert contracts[name]["allow_indirect_imports"] is False, name


def test_schemas_sit_above_models_in_the_layer_order() -> None:
    layers = _contracts()[C_LAYERS]["layers"]
    assert layers.index("schemas") < layers.index("models")


def test_stale_ignore_fails_the_contract() -> None:
    # app.models.user_llm_settings -> app.services.bedrock_settings is an ignored edge.
    # Removing the import in the copy makes the ignore stale, which must be an error,
    # not a silent pass.
    with _backend_copy() as root:
        target = root / "app/models/user_llm_settings.py"
        source = target.read_text()
        import_line = "from app.services.bedrock_settings import public_view"
        assert import_line in source
        target.write_text(source.replace(import_line, "public_view = str"))
        result = _run(root)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    flat = " ".join(output.split())  # the CLI wraps long lines
    assert "No matches for ignored import" in flat, output
    assert "app.models.user_llm_settings -> app.services.bedrock_settings" in flat, output
