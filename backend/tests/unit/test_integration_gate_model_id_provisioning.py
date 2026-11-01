"""The integration gate must resolve OPENSEARCH_TEST_MODEL_ID from the stack itself.

``test_search_completeness_opensearch.py``'s four hybrid tests skip without
``OPENSEARCH_TEST_MODEL_ID`` and nothing in the repo set it, so they skipped on every gate
run since they landed. ``run-integration-tests.sh`` now resolves the id from the stack's own
backend and verifies on the cluster that the model is DEPLOYED with 384 dimensions (the
test hardcodes 384 in its throwaway index mapping).

The REAL ``# --- BEGIN integration-env ---`` block is extracted and run against a fake
``docker`` and ``curl``, so the control flow, the verification and the warning are the real
ones. A model that is not provably usable must stay unexported: a wrong dimension would fail
the bulk load instead of skipping, and an unverified id would hide a missing model.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"

MODEL_ID = "abc123XYZ"


def _extract_block() -> str:
    text = GATE.read_text(encoding="utf-8")
    start = text.index("# --- BEGIN integration-env ---")
    end = text.index("# --- END integration-env ---", start)
    return text[start : text.index("\n", end) + 1]


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(
    tmp_path: Path,
    *,
    exec_out: str = f"MODELID:{MODEL_ID}\n",
    exec_rc: int = 0,
    model_doc: dict | None = None,
    pre_exported: str | None = None,
) -> tuple[str, str]:
    """Run the block; returns (value of INTEGRATION_ENV joined, combined output)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    doc = (
        model_doc
        if model_doc is not None
        else {
            "model_state": "DEPLOYED",
            "model_config": {"embedding_dimension": 384},
        }
    )
    (tmp_path / "model.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "exec.out").write_text(exec_out, encoding="utf-8")
    _write_exec(
        bin_dir / "docker",
        "#!/bin/bash\n"
        'if [ "$1" = "ps" ]; then\n'
        '  case "$*" in\n'
        '    *service=redis*) echo "127.0.0.1:5177->6379/tcp" ;;\n'
        '    *) echo "fake-backend" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "exec" ]; then\n'
        '  case "$*" in\n'
        '    *printenv*) echo "pw"; exit 0 ;;\n'
        f'    *) cat "{tmp_path}/exec.out"; exit {exec_rc} ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n",
    )
    _write_exec(bin_dir / "curl", f'#!/bin/bash\ncat "{tmp_path}/model.json"\n')
    snippet = f"""
set -uo pipefail
YELLOW=''; NC=''
COMPOSE_PROJECT_NAME=otfresh-fake
{_extract_block()}
echo "ENV=${{INTEGRATION_ENV[*]:-}}"
"""
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("OPENSEARCH_TEST_MODEL_ID", None)
    if pre_exported is not None:
        env["OPENSEARCH_TEST_MODEL_ID"] = pre_exported
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    env_line = next(line for line in proc.stdout.splitlines() if line.startswith("ENV="))
    return env_line[len("ENV=") :], out


@pytest.mark.unit
def test_gate_script_declares_the_integration_env_block() -> None:
    assert "# --- BEGIN integration-env ---" in GATE.read_text(encoding="utf-8")


@pytest.mark.unit
def test_deployed_384_model_is_exported_to_the_integration_phase(tmp_path: Path) -> None:
    value, _ = _run(tmp_path)

    assert value == f"env OPENSEARCH_TEST_MODEL_ID={MODEL_ID}"


@pytest.mark.unit
def test_empty_model_id_is_not_exported_and_warns(tmp_path: Path) -> None:
    value, out = _run(tmp_path, exec_out="MODELID:\n")

    assert value == ""
    assert "no active embedding model" in out


@pytest.mark.unit
def test_wrong_dimension_is_not_exported_and_the_warning_names_it(tmp_path: Path) -> None:
    value, out = _run(
        tmp_path,
        model_doc={"model_state": "DEPLOYED", "model_config": {"embedding_dimension": 768}},
    )

    assert value == ""
    assert "768" in out


@pytest.mark.unit
def test_undeployed_model_is_not_exported(tmp_path: Path) -> None:
    value, out = _run(
        tmp_path,
        model_doc={"model_state": "REGISTERED", "model_config": {"embedding_dimension": 384}},
    )

    assert value == ""
    assert "REGISTERED" in out


@pytest.mark.unit
def test_docker_exec_failure_is_not_exported(tmp_path: Path) -> None:
    value, out = _run(tmp_path, exec_out="", exec_rc=1)

    assert value == ""
    assert "OPENSEARCH_TEST_MODEL_ID not provisioned" in out


@pytest.mark.unit
def test_an_operator_exported_value_is_left_untouched(tmp_path: Path) -> None:
    value, out = _run(tmp_path, pre_exported="operator-choice", exec_out="MODELID:other\n")

    assert value == ""
    assert "not provisioned" not in out
