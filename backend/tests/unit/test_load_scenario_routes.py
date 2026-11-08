"""The Locust load scenario calls only real routes, and refuses anything but a --fresh stack.

``scripts/perf/load/locustfile.py`` is run by hand against an isolated stack, so nothing else
would notice a renamed route: the scenario would just report 404s as "load". This imports the
locustfile against a minimal stand-in for ``locust`` (the real package patches the standard
library with gevent on import and must never load inside the test process), runs every task
with a recording client, and checks each request against ``backend/openapi.json``.
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LOAD_DIR = _REPO / "scripts" / "perf" / "load"
_OPENAPI = _REPO / "backend" / "openapi.json"


class _Hook:
    def add_listener(self, fn: Any) -> Any:
        return fn


class _StubHttpUser:
    abstract = True


def _task(weight: int = 1) -> Any:
    def mark(fn: Any) -> Any:
        fn.locust_task_weight = weight
        return fn

    return mark


def _tag(*names: str) -> Any:
    def mark(fn: Any) -> Any:
        fn.locust_tag_set = set(names)
        return fn

    return mark


class _StubLocust(types.ModuleType):
    """The subset of the locust API the locustfile uses, as inert stand-ins.

    Attributes are declared on the class so mypy checks every use of the stub.
    """

    HttpUser = _StubHttpUser
    task = staticmethod(_task)
    tag = staticmethod(_tag)
    events = types.SimpleNamespace(init_command_line_parser=_Hook(), init=_Hook())

    @staticmethod
    def between(low: float, high: float) -> Any:
        return lambda _user: low


class _Response:
    def __init__(self, path: str, method: str) -> None:
        self.status_code = 200
        self.error = None
        self._path, self._method = path, method

    def json(self) -> dict[str, Any]:
        if self._path == "/api/files" and self._method == "GET":
            return {"items": [{"uuid": "file-a"}, {"uuid": "file-b"}]}
        if self._path == "/api/files/prepare":
            return {"file_id": "file-new", "is_duplicate": 0}
        return {"uuid": "comment-a"}

    def success(self) -> None:
        pass

    def failure(self, reason: str) -> None:
        raise AssertionError(f"the stub never fails a call, got failure {reason!r}")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Cookies(dict):
    pass


class _RecordingClient:
    def __init__(self) -> None:
        self.cookies = _Cookies()
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs["name"]))
        if url == "/api/auth/login":
            # The real login answers with Set-Cookie for the session and the CSRF token.
            self.cookies["csrf_token"] = "token"
            return _Response(url, method)
        if method != "GET":
            assert kwargs["headers"].get("X-CSRF-Token") == "token", f"{method} {url} lacks CSRF"
        return _Response(url, method)


@pytest.fixture
def load_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "locust", _StubLocust("locust"))
    monkeypatch.syspath_prepend(str(_LOAD_DIR))
    modules = {}
    for name in ("scenario", "locustfile"):
        spec = importlib.util.spec_from_file_location(name, _LOAD_DIR / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules


def _openapi_routes() -> list[tuple[str, re.Pattern[str]]]:
    paths = json.loads(_OPENAPI.read_text())["paths"]
    routes = []
    for template, operations in paths.items():
        pattern = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$")
        routes += [(method.upper(), pattern) for method in operations]
    return routes


def _run_every_task(locustfile: Any) -> _RecordingClient:
    client = _RecordingClient()
    options = types.SimpleNamespace(request_timeout=5.0, seeded_users=50)
    saved = random.getstate()
    random.seed(1337)  # the scenario draws from the module RNG; fixed so every run is identical
    try:
        for cls in (locustfile.OrdinaryUser, locustfile.HeavyOwner, locustfile.Administrator):
            user = cls.__new__(cls)
            user.environment = types.SimpleNamespace(parsed_options=options)
            user.client = client
            user.on_start()
            tasks = [
                getattr(user, name)
                for name in dir(cls)
                if hasattr(getattr(cls, name), "locust_task_weight")
            ]
            assert tasks, f"{cls.__name__} has no tasks"
            for _ in range(60):  # enough draws to reach every random branch
                for run in tasks:
                    run()
    finally:
        random.setstate(saved)
    return client


def test_every_request_the_scenario_makes_is_a_real_route(load_modules):
    client = _run_every_task(load_modules["locustfile"])
    routes = _openapi_routes()
    unknown = sorted(
        {
            f"{method} {url}"
            for method, url, _ in client.calls
            if not any(m == method and p.match(url) for m, p in routes)
        }
    )
    assert not unknown, f"not in backend/openapi.json: {unknown}"


def test_every_declared_route_exists_and_is_exercised(load_modules):
    scenario = load_modules["scenario"]
    routes = _openapi_routes()
    for key, (method, template) in scenario.ROUTES.items():
        concrete = re.sub(r"\{[^}]+\}", "x", template)
        assert any(m == method and p.match(concrete) for m, p in routes), (key, method, template)
    client = _run_every_task(load_modules["locustfile"])
    used = {name.split(" ")[0] for _, _, name in client.calls}
    assert used == set(scenario.ROUTES), set(scenario.ROUTES) ^ used


def test_writes_clean_up_after_themselves(load_modules):
    client = _run_every_task(load_modules["locustfile"])
    methods = [(m, url) for m, url, _ in client.calls]
    created_comments = methods.count(
        ("POST", "/api/comments/files/file-a/comments")
    ) + methods.count(("POST", "/api/comments/files/file-b/comments"))
    assert created_comments > 0
    assert methods.count(("DELETE", "/api/comments/comment-a")) == created_comments
    prepared = methods.count(("POST", "/api/files/prepare"))
    assert prepared > 0
    assert methods.count(("DELETE", "/api/files/file-new/purge")) == prepared
    assert ("POST", "/api/files/complete") not in methods, (
        "an upload must never dispatch processing"
    )


@pytest.mark.parametrize(
    ("offset", "host", "expected"),
    [
        (None, None, "missing"),
        ("0", None, "share the dev ports"),
        ("abc", None, "share the dev ports"),
        ("300", "http://localhost:5174", "is not the"),
        ("300", None, None),
        ("300", "http://localhost:5474", None),
    ],
)
def test_the_target_must_be_a_fresh_stack(load_modules, tmp_path, offset, host, expected):
    scenario = load_modules["scenario"]
    if offset is not None:
        (tmp_path / ".fresh").mkdir()
        (tmp_path / ".fresh" / "perf1337.offset").write_text(offset + "\n")
    if expected is None:
        target = scenario.resolve_target("perf1337", host, repo_root=tmp_path)
        assert target.base_url == "http://localhost:5474"
    else:
        with pytest.raises(scenario.NotIsolatedError, match=expected):
            scenario.resolve_target("perf1337", host, repo_root=tmp_path)


def test_no_fresh_name_is_refused(load_modules):
    with pytest.raises(load_modules["scenario"].NotIsolatedError, match="--fresh NAME is required"):
        load_modules["scenario"].resolve_target("", None)
