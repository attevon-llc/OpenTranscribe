"""Tests for ``scripts/lane-check.sh`` and the hook-entry resolver it is built on.

lane-check runs each pre-commit-backed guard through the hook's own ``entry``, so the two things
worth pinning are: the resolver refuses anything it cannot run faithfully (unknown id, a hook that
is handed filenames), and the runner reports a failing guard as FAIL with a non-zero exit instead
of losing it in the parallel fan-out.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "lane-check.sh"
_RESOLVER = _REPO / "scripts" / "lib" / "precommit_entry.py"


def _load():
    spec = importlib.util.spec_from_file_location("_precommit_entry_under_test", _RESOLVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resolver = _load()


def _config(**hooks: dict) -> dict:
    local = [{"id": hook_id, **body} for hook_id, body in hooks.items()]
    return {
        "repos": [
            {"repo": "https://github.com/example/remote", "rev": "v1", "hooks": [{"id": "remote"}]},
            {"repo": "local", "hooks": local},
        ]
    }


def test_resolver_returns_system_entry_verbatim():
    cfg = _config(
        g={"entry": "python3 scripts/guards/x.py", "language": "system", "pass_filenames": False}
    )
    assert resolver.resolve(cfg, "g", "/venv/python") == "python3 scripts/guards/x.py"


def test_resolver_substitutes_the_interpreter_for_python_hooks():
    cfg = _config(
        g={
            "entry": "python scripts/guards/x.py --flag",
            "language": "python",
            "pass_filenames": False,
        }
    )
    assert resolver.resolve(cfg, "g", "/venv/python") == "/venv/python scripts/guards/x.py --flag"


@pytest.mark.parametrize(
    ("hook_id", "body"),
    [
        ("missing", None),
        ("remote", None),
        ("files", {"entry": "ruff", "language": "system"}),
        ("odd", {"entry": "bash -c x", "language": "python", "pass_filenames": False}),
    ],
)
def test_resolver_refuses_what_it_cannot_run_faithfully(hook_id, body):
    cfg = _config(**({hook_id: body} if body else {}))
    with pytest.raises(resolver.EntryError):
        resolver.resolve(cfg, hook_id, "/venv/python")


def _lane_check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LANE_CHECK_TAIL": "5"},
    )


def test_list_resolves_every_guard_from_the_real_config():
    listed = _lane_check("--list")
    assert listed.returncode == 0, listed.stderr
    names = [line.split("\t", 1)[0] for line in listed.stdout.splitlines()]
    for expected in (
        "duplicates",
        "hidden-mypy",
        "unused-css",
        "check-i18n",
        "audit-tests",
        "test-audit",
    ):
        assert expected in names
    real = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    line = next(x for x in listed.stdout.splitlines() if x.startswith("style-literals\t"))
    assert line.split("\t", 1)[1] == resolver.resolve(real, "guard-style-literals")


def test_unknown_guard_name_is_misuse():
    result = _lane_check("--only", "no-such-guard")
    assert result.returncode == 2
    assert "unknown guard" in result.stderr


def test_a_renamed_hook_fails_before_anything_runs(tmp_path):
    real = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    for repo in real["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == "guard-raw-env":
                hook["id"] = "guard-raw-env-renamed"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(real), encoding="utf-8")
    result = _lane_check("--config", str(cfg), "--list")
    assert result.returncode == 2
    assert "guard-raw-env" in result.stderr


def test_a_failing_guard_is_reported_and_fails_the_run(tmp_path):
    real = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    for repo in real["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == "guard-raw-env":
                hook["entry"] = "bash -c 'echo planted-violation >&2; exit 1'"
            if hook.get("id") == "guard-api-boundary":
                hook["entry"] = "bash -c 'echo clean'"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(real), encoding="utf-8")
    result = _lane_check("--config", str(cfg), "--only", "raw-env,api-boundary")
    assert result.returncode == 1
    assert "FAIL  raw-env" in result.stdout
    assert "PASS  api-boundary" in result.stdout
    assert "planted-violation" in result.stdout
    assert "2 guard(s), 1 failed" in result.stdout


def test_all_green_selection_exits_zero(tmp_path):
    real = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    for repo in real["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") in {"guard-raw-env", "guard-api-boundary"}:
                hook["entry"] = "true"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(real), encoding="utf-8")
    result = _lane_check("--config", str(cfg), "--only", "raw-env,api-boundary")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 guard(s), 0 failed" in result.stdout
