"""Non-HTTP layers read the request through ``RequestScope``, not through a web framework type."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from app.core.request_scope import RequestScope
from app.utils.client_ip import UNKNOWN
from app.utils.client_ip import resolve_client_ip

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
FRAMEWORK_FREE = (
    "core/capabilities.py",
    "core/locked_settings.py",
    "utils/client_ip.py",
    "auth/provider_registry.py",
    "services/content_audit.py",
    "services/auth_config_service.py",
    "services/account_security_service.py",
)


def _framework_imports(rel: str) -> list[str]:
    tree = ast.parse((APP_ROOT / rel).read_text())
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {
            "fastapi",
            "starlette",
        }:
            found.append(node.module or "")
        elif isinstance(node, ast.Import):
            found += [
                a.name for a in node.names if a.name.split(".")[0] in {"fastapi", "starlette"}
            ]
    return found


def test_framework_free_modules_import_no_web_framework():
    assert {rel: _framework_imports(rel) for rel in FRAMEWORK_FREE if _framework_imports(rel)} == {}


def test_a_plain_object_with_client_and_headers_resolves_a_client_ip():
    scope: RequestScope = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.9"), headers={}, state=SimpleNamespace()
    )
    assert resolve_client_ip(scope) == "203.0.113.9"
    assert resolve_client_ip(None) == UNKNOWN


def test_a_real_starlette_request_is_a_request_scope():
    request = Request({"type": "http", "headers": [], "client": ("198.51.100.4", 5000)})
    scope: RequestScope = request
    assert resolve_client_ip(scope) == "198.51.100.4"
