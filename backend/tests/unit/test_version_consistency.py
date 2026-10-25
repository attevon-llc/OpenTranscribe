"""Version consistency, enforced by the existing CI backend-tests job.

``scripts/release/check-version-consistency.py`` is the single implementation.
This test only shells it in ``ci`` mode so the check runs on every PR without
anyone having to remember it — which is precisely what went wrong before:
``expected-schemas.tsv`` had a documented "append a row every release" rule and
no automated enforcement, so v0.4.1's row was simply never added.

The script is deliberately stdlib-only so it also runs in environments that have
no venv (bare CI runners, the release orchestrator's preflight).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "release" / "check-version-consistency.py"

pytestmark = pytest.mark.skipif(
    not CHECKER.exists(), reason="release tooling not present in this checkout"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_version_sources_are_consistent():
    """VERSION, pyproject, package.json, package-lock, alembic head, blog slugs."""
    proc = _run("--mode", "ci")
    assert proc.returncode == 0, f"version consistency check failed:\n{proc.stdout}\n{proc.stderr}"


def test_json_output_is_machine_readable():
    """Agents branch on this contract; a schema change must break a test, not a bot."""
    proc = _run("--mode", "ci", "--json")
    payload = json.loads(proc.stdout)

    assert payload["stage"] == "version-consistency"
    assert payload["status"] in {"pass", "fail"}
    assert payload["version"].startswith("v")
    assert isinstance(payload["criteria"], list) and payload["criteria"]
    assert isinstance(payload["next"], list)

    for criterion in payload["criteria"]:
        assert {"id", "status", "summary", "detail", "fix"} <= criterion.keys()
        assert criterion["status"] in {"pass", "fail", "warn", "skip"}


def test_exit_code_two_on_misuse():
    """Distinct exit codes let an agent tell "gate failed" from "I called it wrong"."""
    assert _run("--mode", "not-a-mode").returncode == 2


def _load_checker():
    """Import the dash-named script as a module so one check can run against a fixture tree."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_version_consistency", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _frontend_git_sha_result(monkeypatch, tmp_path: Path, dockerfile_text: str):
    checker = _load_checker()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "Dockerfile.prod").write_text(dockerfile_text, encoding="utf-8")
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    ctx = checker.Ctx(mode="ci")
    checker.check_frontend_git_sha_arg(ctx)
    (result,) = ctx.results
    return result


@pytest.mark.parametrize(
    ("dockerfile_text", "missing"),
    [
        ("FROM node\nRUN npm run build\n", "ARG GIT_SHA, ENV GIT_SHA"),
        # An ARG alone never reaches vite: process.env only sees the ENV line.
        ("FROM node\nARG GIT_SHA=unknown\nRUN npm run build\n", "ENV GIT_SHA"),
        ("FROM node\nENV GIT_SHA=x\nRUN npm run build\n", "ARG GIT_SHA"),
    ],
)
def test_frontend_git_sha_arg_check_fails_when_incomplete(
    monkeypatch, tmp_path, dockerfile_text, missing
):
    """#1273: without ARG+ENV GIT_SHA the bundle bakes `unknown` and the stale-tab check goes blind."""
    result = _frontend_git_sha_result(monkeypatch, tmp_path, dockerfile_text)

    assert result.status == "fail"
    assert result.detail == f"missing: {missing}"


def test_frontend_git_sha_arg_check_passes_when_declared_and_exported(monkeypatch, tmp_path):
    result = _frontend_git_sha_result(
        monkeypatch,
        tmp_path,
        "FROM node\nARG GIT_SHA=unknown\nENV GIT_SHA=${GIT_SHA}\nRUN npm run build\n",
    )

    assert result.status == "pass"


def test_real_frontend_dockerfile_passes_the_git_sha_check():
    """The shipped Dockerfile.prod, through the shipped check, in the shipped criteria list."""
    payload = json.loads(_run("--mode", "ci", "--json").stdout)
    by_id = {c["id"]: c for c in payload["criteria"]}

    assert by_id["dockerfile-frontend-git-sha-arg"]["status"] == "pass"
