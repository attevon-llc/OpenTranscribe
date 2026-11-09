"""Every Gauge declares its ``multiprocess_mode``; no custom collector is registered.

prometheus_client multiprocess mode ("Multiprocess Mode", gauges): a Gauge defaults to
``all``, which exports one series per process with a ``pid`` label, and custom collectors are
not supported at all. Both would make ``/metrics`` wrong or noisy once the API runs several
workers, so the rule is enforced over the whole application tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"


def _called_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def bare_gauges(source: str) -> list[int]:
    """Line numbers of ``Gauge(...)`` calls without a ``multiprocess_mode`` keyword."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and _called_name(node) == "Gauge"
        and not any(kw.arg == "multiprocess_mode" for kw in node.keywords)
    ]


def default_registry_registrations(source: str) -> list[int]:
    """Line numbers of ``REGISTRY.register(...)``: a custom collector on the default registry."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and _called_name(node) == "register"
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "REGISTRY"
    ]


def test_the_detector_fires_on_a_bare_gauge_and_stays_quiet_on_a_declared_one():
    assert bare_gauges('x = Gauge("a", "help")\n') == [1]
    assert bare_gauges('x = Gauge("a", "help", multiprocess_mode="livesum")\n') == []
    assert default_registry_registrations("REGISTRY.register(Foo())\n") == [1]
    assert default_registry_registrations("other.register(Foo())\n") == []


def test_every_gauge_in_the_application_declares_a_multiprocess_mode():
    offenders = {
        f"{path.relative_to(_APP)}:{line}"
        for path in _APP.rglob("*.py")
        for line in bare_gauges(path.read_text())
    }
    assert not offenders, f"Gauge without multiprocess_mode: {sorted(offenders)}"


def test_no_custom_collector_is_registered_on_the_default_registry():
    offenders = {
        f"{path.relative_to(_APP)}:{line}"
        for path in _APP.rglob("*.py")
        for line in default_registry_registrations(path.read_text())
    }
    assert not offenders, f"custom collector on REGISTRY: {sorted(offenders)}"
