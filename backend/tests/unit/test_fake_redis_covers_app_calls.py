"""The shared ``FakeRedis`` answers every command the app sends through the Redis factories.

``tests/unit/_fake_liveness_redis.py`` is installed for every ``get_redis()`` and
``get_request_redis()`` call in the tests that use it, so a code path that starts sending a new
command breaks those tests with an ``AttributeError`` raised from the fake, far from the change
that caused it. That happened when ``retrieval_cache.bump_corpus_version`` started calling
``incr``: eleven export tests failed at once. This test finds the drift at the change instead.

It reads ``app/`` statically: within each function (and at module level) a name bound to
``get_redis()`` / ``get_request_redis()``, or to ``.pipeline()`` on such a name, is a client,
and every method called on a client (or directly on the factory call) is a Redis command the
fake must implement. A client handed in as a parameter or kept on ``self`` is not traced; those
paths are tested with their own stand-ins or the real Redis.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from pathlib import Path

import redis

from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import _FakePipeline

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
FACTORIES = frozenset({"get_redis", "get_request_redis"})

#: Commands the app sends that the fake deliberately does not implement, with the reason.
NOT_FAKED = {
    "eval": (
        "server-side Lua (auth/revocation_backlog.py and friends); a dict cannot run a script, "
        "so those paths are tested against the real Redis"
    ),
}

#: Methods on the fake that are test helpers, not Redis commands.
HELPERS = frozenset({"lapse", "mark_queued", "mark_running"})


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _client_names(scope: ast.AST) -> set[str]:
    """Names bound in ``scope`` to a factory client or to a pipeline on one."""
    names: set[str] = set()
    assigns = [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
    ]
    grew = True
    while grew:
        before = len(names)
        for node in assigns:
            call = node.value
            assert isinstance(call, ast.Call)
            is_factory = _called_name(call) in FACTORIES
            is_pipeline = (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "pipeline"
                and _is_client(call.func.value, names)
            )
            if is_factory or is_pipeline:
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        grew = len(names) > before
    return names


def _is_client(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Call):
        return _called_name(node) in FACTORIES
    return isinstance(node, ast.Name) and node.id in names


def redis_commands_called(source: str) -> set[str]:
    """Every method called on a factory client in ``source``."""
    tree = ast.parse(source)
    scopes = [tree] + [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    commands: set[str] = set()
    for scope in scopes:
        names = _client_names(scope)
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _is_client(node.func.value, names)
            ):
                commands.add(node.func.attr)
    return commands


def _app_commands() -> set[str]:
    commands: set[str] = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        commands |= redis_commands_called(path.read_text(encoding="utf-8"))
    return commands


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, _member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def _fake_methods() -> set[str]:
    return _public_methods(FakeRedis)


def test_the_scanner_finds_direct_assigned_and_pipelined_commands():
    source = (
        "from app.core.redis import get_redis, get_request_redis\n"
        "def f():\n"
        "    get_request_redis().incr('k')\n"
        "    r = get_redis()\n"
        "    r.hincrby('h', 'f', 1)\n"
        "    pipe = r.pipeline()\n"
        "    pipe.sadd('s', 'v')\n"
        "    pipe.execute()\n"
    )
    assert redis_commands_called(source) == {"incr", "hincrby", "pipeline", "sadd", "execute"}


def test_the_scanner_ignores_calls_on_other_receivers():
    source = (
        "def f(client, db):\n"
        "    client.incr('k')\n"
        "    db.expire(obj)\n"
        "    r = make_redis()\n"
        "    r.get('k')\n"
    )
    assert redis_commands_called(source) == set()


def test_the_app_scan_is_not_vacuous():
    # bump_corpus_version's incr and the liveness markers' setex are known call sites.
    assert {"incr", "setex", "hset"} <= _app_commands()


def test_every_command_the_app_sends_is_implemented_by_the_fake():
    # watch / multi / unwatch / execute are sent on a pipeline, so the fake pipeline answers them.
    implemented = _fake_methods() | _public_methods(_FakePipeline)
    missing = _app_commands() - implemented - set(NOT_FAKED)
    assert missing == set(), (
        f"app code sends {sorted(missing)} through get_redis()/get_request_redis(), and "
        "tests/unit/_fake_liveness_redis.py's FakeRedis does not implement it; add the "
        "command to the fake (or to NOT_FAKED here, with the reason it cannot be faked)"
    )


def test_every_fake_command_is_a_real_redis_command():
    invented = (_fake_methods() - HELPERS - set(dir(redis.Redis))) | (
        _public_methods(_FakePipeline) - set(dir(redis.client.Pipeline))
    )
    assert invented == set(), f"FakeRedis methods that redis.Redis does not have: {invented}"


def test_no_helper_shadows_a_real_redis_command():
    shadowing = HELPERS & set(dir(redis.Redis))
    assert shadowing == set(), (
        f"test helpers {shadowing} share a name with a Redis command, so a code path sending "
        "that command would get the helper's behaviour instead"
    )


def _positional(func) -> list[inspect.Parameter]:
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return [p for p in inspect.signature(func).parameters.values() if p.kind in kinds][1:]


def signature_drift(fake_cls: type, real_cls: type) -> dict[str, str]:
    """Shared methods whose positional parameters differ from the real client's.

    A fake method must take the real command's parameters in the real order (a caller passing
    ``ex`` third, as redis-py allows, must not land in ``nx``) and must not demand more than the
    real command does. ``*args`` stands in for the real variadic tail. A positional-only fake
    parameter may be named differently: no keyword call can reach it by the wrong name.
    """
    drift: dict[str, str] = {}
    for name in _public_methods(fake_cls) & set(dir(real_cls)):
        fake = _positional(getattr(fake_cls, name))
        real = _positional(getattr(real_cls, name))
        renamed = [
            f"{f.name} (real: {r.name if r else 'none'})"
            for f, r in itertools.zip_longest(fake, real[: len(fake)])
            if f.kind is not inspect.Parameter.POSITIONAL_ONLY and (r is None or f.name != r.name)
        ]
        if renamed:
            drift[name] = f"parameters out of line with the real command: {renamed}"
            continue
        required_real = sum(p.default is inspect.Parameter.empty for p in real)
        variadic = any(
            p.kind is inspect.Parameter.VAR_POSITIONAL
            for p in inspect.signature(getattr(fake_cls, name)).parameters.values()
        )
        if len(fake) < required_real and not variadic:
            drift[name] = f"fake takes {len(fake)} positional, real requires {required_real}"
    return drift


def test_the_signature_check_catches_a_reordered_and_a_short_method():
    class Real:
        def set(self, name, value, ex=None, nx=False): ...

        def expire(self, name, time): ...

    class Fake:
        def set(self, name, value, nx=False, ex=None): ...

        def expire(self, key): ...

    class PositionalOnlyFake:
        def set(self, name, value, expiry=None, /): ...

        def expire(self, name, time): ...

    assert set(signature_drift(Fake, Real)) == {"set", "expire"}
    assert signature_drift(PositionalOnlyFake, Real) == {}


def test_fake_commands_take_the_real_parameters():
    drift = signature_drift(FakeRedis, redis.Redis) | signature_drift(
        _FakePipeline, redis.client.Pipeline
    )
    assert drift == {}, drift


def test_the_not_faked_table_names_only_commands_the_app_still_sends():
    stale = set(NOT_FAKED) - _app_commands()
    assert stale == set(), f"NOT_FAKED entries the app no longer sends: {stale}"
