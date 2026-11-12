"""The route half of assert-inert (issue #898): model the real app's routes.

Starlette route types are HTTP-layer knowledge, so this lives in ``app/api`` (the
``guard-api-boundary`` rule) while the framework-free parts — the publish counter, the beat
classification and the report — live in ``app/core/inert.py``, whose docstring explains the
whole checker. The entry point is ``python -m app.scripts.assert_inert``.

The model branches on the route type explicitly. Starlette's ``BaseHTTPMiddleware.__call__``
passes every non-``http`` scope straight to the app, so a ``WebSocketRoute`` is never seen by
HTTP middleware; it is reported as such, and a route type the model does not know fails
closed (Starlette docs, "Routing": ``Route``, ``WebSocketRoute``, ``Mount``).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount
from starlette.routing import Route
from starlette.routing import WebSocketRoute

from app.core.celery_beat import BEAT_ENTRY_EFFECT
from app.core.inert import MIN_ROUTES_MODELLED
from app.core.inert import SAFE_METHODS
from app.core.inert import WEBSOCKET
from app.core.inert import InertReport
from app.core.inert import RouteFinding
from app.core.inert import active_beat_entries
from app.core.inert import probe_demo_publish_refusal
from app.core.inert import probe_publish_hook
from app.core.inert import render
from app.core.inert import unclassified_beat_entries
from app.core.inert_reach import APP_PACKAGES
from app.core.inert_reach import ReachabilityModel


def _dependency_calls(dependant: Any, seen: set[int] | None = None) -> list[Any]:
    seen = set() if seen is None else seen
    if dependant is None or id(dependant) in seen:
        return []
    seen.add(id(dependant))
    calls = [dependant.call] if getattr(dependant, "call", None) is not None else []
    for sub in getattr(dependant, "dependencies", None) or []:
        calls += _dependency_calls(sub, seen)
    return calls


def _first_witness(model: ReachabilityModel, targets: list[Any]) -> str | None:
    for target in targets:
        found = model.witness(target)
        if found is not None:
            return found.render()
    return None


def model_routes(
    routes: list[Any], model: ReachabilityModel | None = None, prefix: str = ""
) -> list[RouteFinding]:
    """Model every route; each publish-reaching route carries a rendered witness chain."""
    model = model or ReachabilityModel()
    out: list[RouteFinding] = []
    for route in routes:
        path = prefix + getattr(route, "path", "")
        if isinstance(route, WebSocketRoute):
            targets = [route.endpoint, *_dependency_calls(getattr(route, "dependant", None))]
            out.append(
                RouteFinding(
                    kind="websocket",
                    methods=(WEBSOCKET,),
                    path=path,
                    endpoint=_qualname(route.endpoint),
                    bypasses_http_middleware=True,
                    witness=_first_witness(model, targets),
                )
            )
        elif isinstance(route, Route):
            targets = [route.endpoint, *_dependency_calls(getattr(route, "dependant", None))]
            out.append(
                RouteFinding(
                    kind="http",
                    methods=tuple(sorted(route.methods or {"GET"})),
                    path=path,
                    endpoint=_qualname(route.endpoint),
                    bypasses_http_middleware=False,
                    witness=_first_witness(model, targets),
                )
            )
        elif isinstance(route, Mount) and route.routes:
            out += model_routes(list(route.routes), model, prefix=path)
        else:
            # A mounted ASGI app or an unknown route type: the model cannot see inside it.
            out.append(
                RouteFinding(
                    kind="unmodelled",
                    methods=(WEBSOCKET, *sorted(SAFE_METHODS)),
                    path=path or repr(route),
                    endpoint=type(route).__name__,
                    bypasses_http_middleware=True,
                    witness="unmodelled route type: its handlers cannot be walked",
                )
            )
    return out


def model_middleware(
    user_middleware: list[Any], model: ReachabilityModel | None = None
) -> list[tuple[str, str, str]]:
    """``(middleware, scope, witness)`` for each app middleware that reaches a publish.

    ``scope`` is ``"http"`` for a ``BaseHTTPMiddleware`` subclass (it never sees a WebSocket)
    and ``"http+websocket"`` for a pure ASGI middleware.
    """
    model = model or ReachabilityModel()
    found = []
    for entry in user_middleware:
        cls = getattr(entry, "cls", None)
        if not isinstance(cls, type):
            continue
        witness = _first_witness(model, model.callees_of_class(cls))
        if witness is not None:
            scope = "http" if issubclass(cls, BaseHTTPMiddleware) else "http+websocket"
            found.append((_qualname(cls), scope, witness))
    return found


def _qualname(obj: Any) -> str:
    return f"{getattr(obj, '__module__', '?')}.{getattr(obj, '__qualname__', repr(obj))}"


def check(
    app: Any, packages: tuple[str, ...] = APP_PACKAGES, min_routes: int = MIN_ROUTES_MODELLED
) -> InertReport:
    """Run all three checks against ``app`` (a FastAPI/Starlette application).

    ``packages`` names whose code the route model walks (tests add their own module).
    """
    model = ReachabilityModel(packages=packages)
    findings = model_routes(list(app.routes), model)
    middleware = model_middleware(list(getattr(app, "user_middleware", []) or []), model)
    return InertReport(
        hook_measured=probe_publish_hook(),
        publishing_safe_routes=[f for f in findings if f.safe and f.witness],
        publishing_middleware=middleware,
        unclassified_beat=unclassified_beat_entries(),
        beat_effects={name: str(effect) for name, effect in sorted(BEAT_ENTRY_EFFECT.items())},
        routes_modelled=len(findings),
        unresolved_imports=sorted(model.unresolved_imports),
        min_routes=min_routes,
        demo_publish_refused=probe_demo_publish_refusal(),
        active_beat=active_beat_entries(),
    )


def main(argv: list[str] | None, app: Any, packages: tuple[str, ...] = APP_PACKAGES) -> int:
    """Parse ``[--json]``, check ``app`` and print the report; returns the exit code.

    The entry point that loads the real app is ``app/scripts/assert_inert.py``: ``app.core``
    may not import ``app.main`` (import-linter layer contracts).
    """
    parser = argparse.ArgumentParser(prog="python -m app.scripts.assert_inert")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--min-routes",
        type=int,
        default=MIN_ROUTES_MODELLED,
        help="route-count floor; fewer modelled routes reports NOT MEASURED",
    )
    args = parser.parse_args(argv)
    report = check(app, packages, args.min_routes)
    print(json.dumps(report.to_dict(), indent=2) if args.json else render(report))
    return report.exit_code
