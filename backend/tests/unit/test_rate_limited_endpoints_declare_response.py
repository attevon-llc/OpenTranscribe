"""Every rate-limited endpoint must declare ``response: Response``.

The limiter is built with ``headers_enabled=True`` (``app/auth/rate_limit.py``),
so slowapi injects ``X-RateLimit-*`` into a ``response`` parameter after the
handler runs. When the decorated function has no such parameter, slowapi looks at
the return value instead, and raises

    Exception: parameter `response` must be an instance of starlette.responses.Response

for anything that is not already a ``Response``. An endpoint returning a Pydantic
model — which is most of them — therefore answers **500 on every call**.

This is invisible to the rest of the suite. Unit tests reach these handlers
through ``_unwrap()``, which strips the slowapi decorator precisely so the
function can be called directly, so the failing code path is never executed. It
took an HTTP request through the real middleware stack in a container to surface
it, and by then ``GET /api/auth/methods`` — the endpoint the login page calls to
decide which sign-in options to render — was returning 500 on a branch whose
whole purpose was hardening authentication.

The rule is cheap to satisfy and cheap to check, so it is checked here rather
than trusted. Adding ``@limiter.limit`` to a handler without adding the parameter
now fails the suite instead of the deployment.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _is_rate_limited(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether *node* carries a ``@limiter.limit(...)`` decorator."""
    return any(
        "limiter" in ast.unparse(d) and "limit" in ast.unparse(d) for d in node.decorator_list
    )


def _annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
    """Map parameter name → annotation source for *node*."""
    return {
        arg.arg: (ast.unparse(arg.annotation) if arg.annotation else "")
        for arg in node.args.args + node.args.kwonlyargs
    }


def _rate_limited_handlers() -> list[tuple[str, int, str, dict[str, str]]]:
    """Every ``@limiter.limit`` handler under ``app/``, with its parameters."""
    found: list[tuple[str, int, str, dict[str, str]]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_rate_limited(node):
                rel = str(path.relative_to(APP_ROOT.parent))
                found.append((rel, node.lineno, node.name, _annotations(node)))
    return found


def test_every_rate_limited_handler_declares_a_response_parameter():
    offenders = [
        f"{rel}:{lineno} {name}"
        for rel, lineno, name, params in _rate_limited_handlers()
        if params.get("response") != "Response"
    ]
    assert not offenders, (
        "These handlers are rate-limited but do not declare `response: Response`. "
        "slowapi cannot inject its headers and will raise a 500 for any handler "
        "returning a Pydantic model:\n  " + "\n  ".join(offenders)
    )


def test_every_rate_limited_handler_declares_a_request_parameter():
    """The sibling requirement, which slowapi enforces with the same shape of error."""
    offenders = [
        f"{rel}:{lineno} {name}"
        for rel, lineno, name, params in _rate_limited_handlers()
        if params.get("request") != "Request"
    ]
    assert not offenders, (
        "These handlers are rate-limited but do not declare `request: Request`:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_actually_finds_handlers():
    """Guard the guard: a scanner that matches nothing would pass both tests above."""
    handlers = _rate_limited_handlers()
    assert len(handlers) >= 20, (
        f"only {len(handlers)} rate-limited handlers found — the decorator detection "
        "has probably drifted, which would make the checks above vacuous"
    )


def test_headers_enabled_is_what_makes_this_required():
    """If header injection is ever turned off, this file explains itself.

    Pin the coupling so the next person to touch the limiter sees why the
    parameter is mandatory rather than deleting it as noise.
    """
    source = (APP_ROOT / "auth" / "rate_limit.py").read_text()
    assert '"headers_enabled": True' in source, (
        "headers_enabled is no longer True. The `response: Response` requirement "
        "above comes from it — re-read this module's docstring before relaxing "
        "either one."
    )
