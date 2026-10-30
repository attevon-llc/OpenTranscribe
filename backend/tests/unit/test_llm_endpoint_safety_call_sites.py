"""Every handler that persists or dials a caller-chosen LLM ``base_url`` must run the SSRF guard.

The guard lives in ``app.api.endpoints.llm_endpoint_safety``. The endpoint file was split by
concern, so the handlers that need it are spread over several modules; this pins, from the
source, that none of them stops calling it. Behaviour (the guard actually refusing a loopback
URL) is covered by ``tests/api/endpoints/test_llm_settings_routes.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENDPOINTS = Path(__file__).resolve().parents[2] / "app" / "api" / "endpoints"

# (module file, handler, guard it must call)
GUARDED_HANDLERS = [
    ("llm_settings.py", "create_user_llm_configuration", "_assert_safe_llm_endpoint"),
    ("llm_settings.py", "update_user_llm_configuration", "_assert_safe_llm_endpoint"),
    ("llm_connection_tests.py", "test_llm_connection", "_assert_safe_llm_endpoint"),
    ("llm_model_discovery.py", "get_ollama_models", "_pin_llm_endpoint"),
    ("llm_model_discovery.py", "get_openai_compatible_models", "_pin_llm_endpoint"),
]


def _called_names(source: str, function: str) -> set[str]:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
    raise AssertionError(f"{function} not found")


@pytest.mark.parametrize(
    ("module", "handler", "guard"),
    GUARDED_HANDLERS,
    ids=[f"{m}:{h}" for m, h, _ in GUARDED_HANDLERS],
)
def test_handler_calls_the_ssrf_guard(module: str, handler: str, guard: str) -> None:
    source = (ENDPOINTS / module).read_text()
    assert guard in _called_names(source, handler), (
        f"{module}:{handler} no longer calls {guard}; a caller-chosen LLM base_url would "
        "reach the network or the database unchecked (issue #284 A0.1 / #820)."
    )


def test_call_site_detector_reports_a_missing_guard() -> None:
    """Negative control: the detector must say no when the call is absent."""
    clean = "def handler(x):\n    return x\n"
    guarded = "def handler(x):\n    _assert_safe_llm_endpoint(x, 'p')\n"
    assert "_assert_safe_llm_endpoint" not in _called_names(clean, "handler")
    assert "_assert_safe_llm_endpoint" in _called_names(guarded, "handler")


def test_guard_is_defined_only_in_the_safety_module() -> None:
    """A second copy of the guard would drift from the one the tests and callers trust."""
    definers = {
        path.name
        for path in ENDPOINTS.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_assert_safe_llm_endpoint", "_pin_llm_endpoint"}
    }
    assert definers == {"llm_endpoint_safety.py"}
