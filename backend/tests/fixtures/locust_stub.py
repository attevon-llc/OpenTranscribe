"""Run the Locust load scenario (``scripts/perf/load/``) inside the backend test process.

The real ``locust`` package patches the standard library with gevent when imported, so it must
never load here. These inert stand-ins for ``locust``, ``gevent`` and ``websocket`` expose only
what the scenario uses (their attributes are declared on classes so mypy checks every use), and
:class:`RecordingClient` answers every request with a small canned body, so each persona's
tasks can be run and every request it would make recorded.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import random
import re
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
LOAD_DIR = REPO / "scripts" / "perf" / "load"
OPENAPI = REPO / "backend" / "openapi.json"
HOST = "http://localhost:5474"
# Load order: a module's imports come before it.
MODULES = (
    "scenario",
    "routes",
    "profiles",
    "phrasebook",
    "personas_base",
    "personas_read",
    "personas_write",
    "ws_listener",
    "personas_roles",
    "locustfile",
)
ORG_CAPABILITIES = ("organizations", "org.takedown", "org.legal_hold", "org.audit_logs")


class _Hook:
    def add_listener(self, fn: Any) -> Any:
        return fn


class _TaskMeta(type):
    """Mirror of Locust 2.47 ``TaskSetMeta``: ``tasks`` is the bases' tasks, then the class's own.

    Each ``@task`` method is listed ``weight`` times. Subclasses therefore *inherit* every base
    task, even one they override under the same name with a different body.
    """

    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> Any:
        tasks: list[Any] = []
        for base in bases:
            tasks += getattr(base, "tasks", None) or []
        for item in namespace.values():
            if hasattr(item, "locust_task_weight"):
                tasks += [item] * item.locust_task_weight
        namespace["tasks"] = tasks
        return super().__new__(mcs, name, bases, namespace)


class StubUser(metaclass=_TaskMeta):
    abstract = True
    weight = 1
    fixed_count = 0
    host: str | None = None


class StubShape:
    abstract = True
    runner: Any = None
    run_time = 0.0

    def get_run_time(self) -> float:
        return self.run_time


class StopUserError(Exception):
    """Stand-in for ``locust.exception.StopUser``."""


def _task(weight: Any = 1) -> Any:
    if callable(weight):  # bare @task
        weight.locust_task_weight = 1
        return weight

    def mark(fn: Any) -> Any:
        fn.locust_task_weight = weight
        return fn

    return mark


def _tag(*names: str) -> Any:
    def mark(fn: Any) -> Any:
        fn.locust_tag_set = set(names)
        return fn

    return mark


class StubLocust(types.ModuleType):
    FastHttpUser = StubUser
    LoadTestShape = StubShape
    task = staticmethod(_task)
    tag = staticmethod(_tag)
    events = types.SimpleNamespace(init_command_line_parser=_Hook(), init=_Hook())

    @staticmethod
    def between(low: float, high: float) -> Any:
        return lambda _user: low

    @staticmethod
    def constant(seconds: float) -> Any:
        return lambda _user: seconds


class StubLocustException(types.ModuleType):
    StopUser = StopUserError


class StubGevent(types.ModuleType):
    spawned: list[Any] = []

    @staticmethod
    def spawn(fn: Any, *args: Any) -> Any:
        StubGevent.spawned.append(fn)
        return fn


class SocketError(Exception):
    """Stand-in for ``websocket.WebSocketException``."""


class SocketTimeoutError(SocketError):
    """Stand-in for ``websocket.WebSocketTimeoutException``."""


class FakeSocket:
    """A server that greets, echoes every frame it receives and pushes one notification."""

    def __init__(self, url: str, **kwargs: Any) -> None:
        self.url, self.kwargs = url, kwargs
        self.inbox = [json.dumps({"type": "connection_established"})]
        self.inbox.append(json.dumps({"type": "file_status", "data": {}}))
        self.sent: list[str] = []
        self.closed = False

    def recv(self) -> str:
        if not self.inbox:
            raise SocketTimeoutError("quiet")
        return self.inbox.pop(0)

    def send(self, text: str) -> None:
        self.sent.append(text)
        self.inbox.append(json.dumps({"type": "echo", "data": text}))

    def close(self) -> None:
        self.closed = True


class StubWebsocket(types.ModuleType):
    WebSocketException = SocketError
    WebSocketTimeoutException = SocketTimeoutError
    sockets: list[FakeSocket] = []

    @staticmethod
    def create_connection(url: str, **kwargs: Any) -> FakeSocket:
        sock = FakeSocket(url, **kwargs)
        StubWebsocket.sockets.append(sock)
        return sock


@dataclass
class Call:
    persona: str
    method: str
    url: str
    name: str
    params: dict[str, Any]
    task: str
    body: Any = None


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self.error = None
        self._body = body

    def json(self) -> Any:
        return self._body

    def success(self) -> None:
        pass

    def failure(self, reason: str) -> None:
        raise AssertionError(f"the stub never fails a call, got failure {reason!r}")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class _Cookie:
    name: str
    value: str


class _Jar(list):
    def clear(self) -> None:
        del self[:]


# (method, path regex) -> canned JSON body. Lists of dicts carry the ids the tasks pick.
_BODIES: list[tuple[str, str, Any]] = [
    ("GET", r"/api/files", {"items": [{"uuid": f"file-{c}"} for c in "abc"]}),
    ("POST", r"/api/files/prepare", {"file_id": "file-new", "is_duplicate": 0}),
    ("GET", r"/api/files/[^/]+/status-detail", {"status": "completed"}),
    (
        "GET",
        r"/api/files/[^/]+/segments",
        {"transcript_segments": [{"uuid": "seg-1", "text": "hi", "speaker": {"uuid": "spk-1"}}]},
    ),
    ("GET", r"/api/tags", [{"name": "loadtest-mytag-1", "uuid": "tag-1"}]),
    ("GET", r"/api/collections", [{"uuid": "col-1"}]),
    ("GET", r"/api/collections/shared-with-me", [{"uuid": "col-shared"}]),
    (
        "GET",
        r"/api/collections/[^/]+/media",
        {"items": [{"uuid": "file-shared"}], "has_more": False},
    ),
    ("GET", r"/api/speakers", [{"uuid": "spk-1", "name": "SPEAKER_00", "display_name": "P 1"}]),
    ("GET", r"/api/speaker-profiles/profiles", [{"uuid": "prof-1"}]),
    ("GET", r"/api/chat/conversations", {"conversations": [{"uuid": "conv-1"}]}),
    ("GET", r"/api/(admin|org-admin)/holds", {"holds": [{"uuid": "hold-1"}]}),
    ("POST", r"/api/comments/files/[^/]+/comments", {"uuid": "comment-new"}),
    ("POST", r"/api/tags/files/[^/]+/tags", {"uuid": "tag-new"}),
    ("POST", r"/api/collections", {"uuid": "col-new"}),
    ("POST", r"/api/speakers", {"uuid": "spk-new"}),
    ("POST", r"/api/chat/conversations", {"uuid": "conv-new"}),
]


class RecordingClient:
    """Answers like the API would, and records every call with the persona and task making it."""

    def __init__(
        self, capabilities: dict[str, bool], statuses: dict[str, int] | None = None
    ) -> None:
        # path regex -> status the API answers with instead of 200 (e.g. a 204 "nothing to do")
        self.statuses = statuses or {}
        self.cookiejar = _Jar()
        self.calls: list[Call] = []
        self.persona = ""
        self.task = ""
        self.capabilities = capabilities
        self._configs = itertools.count(1)

    def _body(self, method: str, url: str) -> Any:
        if url == "/api/system/capabilities":
            return {"capabilities": self.capabilities}
        if method == "POST" and url == "/api/llm-settings":
            return {"uuid": f"cfg-{next(self._configs)}"}
        for want, pattern, body in _BODIES:
            if want == method and re.fullmatch(pattern, url):
                return body
        return {}

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append(
            Call(
                self.persona,
                method,
                url,
                kwargs["name"],
                dict(kwargs.get("params") or {}),
                self.task,
                kwargs.get("json"),
            )
        )
        if url == "/api/auth/login":
            # The real login answers with Set-Cookie for the session and the CSRF token.
            self.cookiejar[:] = [_Cookie("csrf_token", "token"), _Cookie("access_token", "jwt")]
        elif method != "GET":
            assert kwargs["headers"].get("X-CSRF-Token") == "token", f"{method} {url} lacks CSRF"
        # A correct server refuses the cross-tenant canary (a file the caller does not own).
        default = 404 if "canary" in kwargs["name"] else 200
        if method == "POST" and url.endswith("/export/document"):
            default = 202  # queued, as the real endpoint answers
        status = next((st for pat, st in self.statuses.items() if re.fullmatch(pat, url)), default)
        return _Response(status, self._body(method, url))

    def iter_lines(self, url: str, method: str = "GET", **kwargs: Any) -> Iterator[str]:
        self.request(method, url, **kwargs)
        yield from ("event: status", "data: {}", "", "event: delta", 'data: {"t": "a"}', "")
        yield from ("event: done", "data: {}", "")


@dataclass
class Fired:
    kind: str
    name: str
    error: Any


@dataclass
class Run:
    client: RecordingClient
    fired: list[Fired] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)


def load_modules(monkeypatch: Any) -> dict[str, types.ModuleType]:
    """Import the scenario's modules against the stand-ins; ``monkeypatch`` undoes it all."""
    monkeypatch.setitem(sys.modules, "locust", StubLocust("locust"))
    monkeypatch.setitem(sys.modules, "locust.exception", StubLocustException("locust.exception"))
    monkeypatch.setitem(sys.modules, "gevent", StubGevent("gevent"))
    monkeypatch.setitem(sys.modules, "websocket", StubWebsocket("websocket"))
    monkeypatch.syspath_prepend(str(LOAD_DIR))
    modules = {}
    for name in MODULES:
        spec = importlib.util.spec_from_file_location(name, LOAD_DIR / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules


def options(**overrides: Any) -> types.SimpleNamespace:
    base = {
        "request_timeout": 5.0,
        "seeded_users": 60,
        "pipeline_timeout": 60.0,
        "pipeline_poll": 0.0,
        "mock_llm_url": "http://mock-llm:5199/v1",
        "load_profile": "smoke",
        "target_users": None,
        "ramp_rate": None,
        "hold": None,
        "steps": None,
        "chat_users": None,
    }
    return types.SimpleNamespace(**{**base, **overrides})


def task_methods(cls: type, exclude_tags: frozenset[str] = frozenset()) -> list[Any]:
    """The task functions Locust would run for ``cls`` with ``--exclude-tags`` ``exclude_tags``.

    Taken from ``cls.tasks`` (base-class tasks included, as in Locust), once each.
    """
    funcs: list[Any] = []
    for fn in getattr(cls, "tasks", []):
        if fn not in funcs and not (getattr(fn, "locust_tag_set", set()) & exclude_tags):
            funcs.append(fn)
    return funcs


def run_personas(
    locustfile: types.ModuleType,
    *,
    rounds: int = 40,
    exclude_tags: frozenset[str] = frozenset(),
    capabilities: dict[str, bool] | None = None,
    personas: tuple[str, ...] | None = None,
    statuses: dict[str, int] | None = None,
) -> Run:
    """Start every persona, run each of its tasks ``rounds`` times, then stop it."""
    caps = {cap: True for cap in ORG_CAPABILITIES} if capabilities is None else capabilities
    run = Run(RecordingClient(caps, statuses))
    StubGevent.spawned.clear()
    StubWebsocket.sockets.clear()

    def fire(**kwargs: Any) -> None:
        run.fired.append(Fired(kwargs["request_type"], kwargs["name"], kwargs["exception"]))

    environment = types.SimpleNamespace(
        parsed_options=options(),
        events=types.SimpleNamespace(request=types.SimpleNamespace(fire=fire)),
    )
    saved = random.getstate()
    random.seed(1337)  # the scenario draws from the module RNG; fixed so every run is identical
    try:
        for name, cls in locustfile.USER_CLASSES.items():
            if personas is not None and name not in personas:
                continue
            user = cls.__new__(cls)
            user.environment, user.client, user.host = environment, run.client, HOST
            user._sleep = lambda _s: None
            run.client.persona, run.client.task = name, "on_start"
            try:
                user.on_start()
            except StopUserError:
                run.stopped.append(name)
                continue
            for _ in range(rounds):
                for fn in task_methods(cls, exclude_tags):
                    run.client.task = fn.__name__
                    fn(user)
                    if hasattr(user, "_receive_once"):
                        for _frame in range(3):  # what the receive greenlet would read
                            user._receive_once()
            run.client.task = "on_stop"
            if hasattr(user, "on_stop"):
                user.on_stop()
    finally:
        random.setstate(saved)
    return run


def openapi() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(OPENAPI.read_text())
    return spec


def operation_for(method: str, url: str, spec: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The OpenAPI ``(path template, operation)`` a concrete request resolves to, if any.

    Literal templates win over templated ones (``/api/files/trash`` before
    ``/api/files/{file_uuid}``), as in the router.
    """
    matches = []
    for template, operations in spec["paths"].items():
        op = operations.get(method.lower())
        if op is None:
            continue
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), url):
            matches.append((template.count("{"), template, op))
    if not matches:
        return None
    _, template, op = min(matches)
    return template, op
