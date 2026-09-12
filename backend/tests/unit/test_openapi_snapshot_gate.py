"""Guard the OpenAPI snapshot gate itself (issue #798, step 1).

This is a guard-the-guard test in the shape of ``test_precommit_stage_ci_parity.py``: text/JSON
assertions only. It must NOT import ``app.main`` or regenerate ``backend/openapi.json`` — that
would make the fast unit suite pay the torch/transformers import cost this gate exists to avoid
paying twice (once here, once in CI's dedicated step).
"""

from __future__ import annotations

import ast
import json
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"
SNAPSHOT_PATH = BACKEND_DIR / "openapi.json"
GENERATOR_SCRIPT = REPO_ROOT / "scripts" / "generate-openapi.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pre-commit.yml"

MIN_PATHS = 300
MIN_SCHEMAS = 250
EXPECTED_VERSION = "0.0.0-snapshot"


def _read_snapshot_text() -> str:
    assert SNAPSHOT_PATH.is_file(), f"missing: {SNAPSHOT_PATH}"
    return SNAPSHOT_PATH.read_text(encoding="utf-8")


def test_snapshot_parses_as_json() -> None:
    text = _read_snapshot_text()
    schema = json.loads(text)
    assert isinstance(schema, dict)


def test_snapshot_is_an_openapi_document() -> None:
    schema = json.loads(_read_snapshot_text())
    assert schema["openapi"].startswith("3."), schema.get("openapi")


def test_snapshot_meets_path_and_schema_floors() -> None:
    schema = json.loads(_read_snapshot_text())
    paths = schema.get("paths", {})
    schemas = schema.get("components", {}).get("schemas", {})
    assert len(paths) >= MIN_PATHS, f"only {len(paths)} paths, expected >= {MIN_PATHS}"
    assert len(schemas) >= MIN_SCHEMAS, (
        f"only {len(schemas)} component schemas, expected >= {MIN_SCHEMAS}"
    )


def test_snapshot_version_is_normalized() -> None:
    """info.version must be the constant sentinel, proving the generator normalized it.

    If this ever reads a real semver, the generator's normalize step regressed and the
    snapshot will churn on every release bump (the exact problem the sentinel exists to stop).
    """
    schema = json.loads(_read_snapshot_text())
    assert schema["info"]["version"] == EXPECTED_VERSION


def test_snapshot_has_exactly_one_trailing_newline() -> None:
    text = _read_snapshot_text()
    assert text.endswith("\n"), "snapshot must end with a trailing newline"
    assert not text.endswith("\n\n"), "snapshot must have exactly one trailing newline"


def test_generator_script_is_executable() -> None:
    assert GENERATOR_SCRIPT.is_file(), f"missing: {GENERATOR_SCRIPT}"
    mode = GENERATOR_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"{GENERATOR_SCRIPT} is not executable (chmod +x)"


def test_generator_argparse_group_is_required() -> None:
    """--write/--check must be a REQUIRED mutually-exclusive group.

    This is the structural defence against CI silently regenerating the file it is supposed to
    compare against: a bare invocation with no flag must exit non-zero, never pick a default.
    """
    tree = ast.parse(GENERATOR_SCRIPT.read_text(encoding="utf-8"))
    required_values: list[object] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_mutually_exclusive_group":
            continue
        for keyword in node.keywords:
            if keyword.arg == "required" and isinstance(keyword.value, ast.Constant):
                required_values.append(keyword.value.value)

    assert required_values == [True], (
        "generate-openapi.py must define exactly one mutually-exclusive group with "
        f"required=True; found required= values {required_values!r}"
    )


def test_workflow_runs_check_never_write() -> None:
    """CI must invoke --check, never --write, and never with continue-on-error."""
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "generate-openapi.py --check" in workflow_text
    assert "generate-openapi.py --write" not in workflow_text

    # The step block itself must carry no continue-on-error. Scope the search to the step
    # rather than the whole file, since other steps in this workflow legitimately use it.
    step_marker = "OpenAPI snapshot is current"
    assert step_marker in workflow_text, "expected CI step name not found in workflow"
    step_start = workflow_text.index(step_marker)
    # Look at a bounded window after the step name (up to the next "- name:") for its body.
    next_step = workflow_text.find("\n      - name:", step_start)
    step_body = workflow_text[step_start : next_step if next_step != -1 else len(workflow_text)]
    assert "continue-on-error" not in step_body
