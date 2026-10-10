"""A ``--fresh`` stack's frontend must poll instead of using inotify (issue #961b).

Two Vite dev servers side by side (a fresh stack + the main one) share the
host's ``fs.inotify.max_user_instances`` (128 on the host this was hit on) and
the second one dies with ``EMFILE: too many open files, watch
'/app/vite.config.ts'``. Raising the sysctl needs root, which is not available
to an automated fix; a ``--fresh`` stack is by definition the SECOND watcher,
so it is the one that switches to polling instead.

``fresh_generate_overlay`` now writes ``CHOKIDAR_USEPOLLING=true`` /
``CHOKIDAR_INTERVAL`` into the generated ``.fresh/<name>.yml`` overlay's
``frontend`` service -- unconditionally, regardless of ``--port-offset``,
because the EMFILE hazard has nothing to do with which port is used.

Extracted from the REAL ``fresh_generate_overlay`` (and the two tiny helpers
it calls) and driven via subprocess bash, so a regression in the actual
script fails here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _environment_map(service: dict) -> dict[str, str]:
    """Normalize a compose `environment:` list (`- K=V`) into a dict."""
    out = {}
    for item in service.get("environment", []):
        key, _, value = item.partition("=")
        out[key] = value
    return out


def _generate(tmp_path: Path, *, chokidar_interval: str | None = None) -> dict[str, Any]:
    text = OPENTR.read_text(encoding="utf-8")
    script_parts = [
        _function_body(text, "fresh_sanitize_name"),
        _function_body(text, "fresh_project_name"),
        _function_body(text, "fresh_generate_overlay"),
        "FRESH_NAMED_SERVICES=(frontend)",
        f'FRESH_OVERLAY_DIR="{tmp_path}"',
        'fresh_generate_overlay "myname"',
    ]
    script = "\n".join(script_parts) + "\n"
    env = os.environ.copy()
    if chokidar_interval is not None:
        env["CHOKIDAR_INTERVAL"] = chokidar_interval
    else:
        env.pop("CHOKIDAR_INTERVAL", None)

    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    doc: dict[str, Any] = yaml.safe_load((tmp_path / "myname.yml").read_text(encoding="utf-8"))
    return doc


def test_frontend_gets_chokidar_polling_env_vars(tmp_path: Path):
    doc = _generate(tmp_path)
    env = _environment_map(doc["services"]["frontend"])
    assert env.get("CHOKIDAR_USEPOLLING") == "true"
    assert env.get("CHOKIDAR_INTERVAL") == "300"


def test_chokidar_interval_is_tunable_via_the_environment(tmp_path: Path):
    doc = _generate(tmp_path, chokidar_interval="750")
    env = _environment_map(doc["services"]["frontend"])
    assert env["CHOKIDAR_INTERVAL"] == "750"
