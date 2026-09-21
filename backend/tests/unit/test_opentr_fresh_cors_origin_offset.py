"""A ``--port-offset`` fresh stack must extend ``CORS_ORIGINS``, not just ports.

``--port-offset N`` moves the SPA's own origin (``http://localhost:5173`` ->
``http://localhost:<5173+N>``), but ``backend/app/core/config.py``'s
``CORS_ORIGINS`` default is the two UNOFFSET dev URLs. The WebSocket origin
check (``_origin_is_allowed``, ``backend/app/api/websockets.py`` -- issue
#903's anti-hijacking fix, which must stay exact-match) then rejects every
handshake from an offset stack with 403, and every live feature driven over
``/api/ws`` (upload progress, the notification bell, ...) silently degrades
to a permanent "Reconnecting..." banner (issue #968).

The fix belongs in the allowlist, not the check: ``fresh_generate_overlay``
now appends the offset frontend origins to ``backend``'s ``CORS_ORIGINS`` in
the generated ``.fresh/<name>.yml`` overlay, as a JSON array. JSON, not a
comma-separated string, is load-bearing: pydantic-settings JSON-decodes a
``list[str]`` env var BEFORE the field's ``mode="before"`` validator runs, so
a comma-separated value raises ``SettingsError`` at backend startup (this was
confirmed while triaging the issue, not assumed).

The sibling fix for issue #961b (the fresh stack's ``frontend`` service
getting ``CHOKIDAR_USEPOLLING``) lives in the same generated file and is
pinned separately in ``test_opentr_fresh_chokidar_polling.py``.

Extracted from the REAL ``fresh_generate_overlay`` (and the two tiny helpers
it calls) and driven via subprocess bash, so a regression in the actual
script fails here.
"""

from __future__ import annotations

import json
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


def _generate(tmp_path: Path, *, offset: str, frontend_port: str | None) -> dict[str, Any]:
    text = OPENTR.read_text(encoding="utf-8")
    script_parts = [
        _function_body(text, "fresh_sanitize_name"),
        _function_body(text, "fresh_project_name"),
        _function_body(text, "fresh_generate_overlay"),
    ]
    # Scoped to the two services this fix touches -- the real
    # FRESH_NAMED_SERVICES list is long and irrelevant here.
    script_parts.append("FRESH_NAMED_SERVICES=(backend frontend)")
    script_parts.append(f'FRESH_OVERLAY_DIR="{tmp_path}"')
    script_parts.append(f'fresh_generate_overlay "myname" "{offset}"')
    script = "\n".join(script_parts) + "\n"

    env = os.environ.copy()
    if frontend_port is not None:
        env["FRONTEND_PORT"] = frontend_port
    else:
        env.pop("FRONTEND_PORT", None)
    env.pop("CHOKIDAR_INTERVAL", None)

    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr

    overlay_file = tmp_path / "myname.yml"
    assert overlay_file.exists()
    doc: dict[str, Any] = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
    return doc


def _environment_map(service: dict) -> dict[str, str]:
    """Normalize a compose `environment:` list (`- K=V`) into a dict."""
    out = {}
    for item in service.get("environment", []):
        key, _, value = item.partition("=")
        out[key] = value
    return out


def test_offset_stack_gets_its_own_origins_appended_to_the_defaults(tmp_path: Path):
    doc = _generate(tmp_path, offset="200", frontend_port="5373")
    env = _environment_map(doc["services"]["backend"])
    assert "CORS_ORIGINS" in env, "backend must carry an offset-aware CORS_ORIGINS"

    origins = json.loads(env["CORS_ORIGINS"])
    assert origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5373",
        "http://127.0.0.1:5373",
    ]


def test_cors_origins_is_valid_json_never_a_comma_separated_string(tmp_path: Path):
    """Pins the encoding decision: pydantic-settings JSON-decodes a
    `list[str]` env var BEFORE its `mode="before"` validator runs, so a
    comma-separated value raises SettingsError at backend startup."""
    doc = _generate(tmp_path, offset="200", frontend_port="5373")
    env = _environment_map(doc["services"]["backend"])
    raw = env["CORS_ORIGINS"]
    assert raw.startswith("["), f"expected a JSON array, got: {raw!r}"
    parsed = json.loads(raw)  # raises if this ever regresses to CSV
    assert all(isinstance(o, str) for o in parsed)


def test_offset_zero_adds_no_cors_origins_override(tmp_path: Path):
    """At offset 0 the stack runs on the default port, already covered by
    CORS_ORIGINS' own default -- no override needed."""
    doc = _generate(tmp_path, offset="0", frontend_port="5173")
    env = _environment_map(doc["services"]["backend"])
    assert "CORS_ORIGINS" not in env
