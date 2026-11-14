"""An explicit ``--redis-url`` works with no stack running; the stack is consulted only without it.

``bench_common.load_redis_url`` refuses when no stack publishes a Redis port. The benchmark
packages used to call it at import to fill argparse defaults, so that refusal also stopped a
run that named its Redis outright. Per the argparse docs the default is ``None`` and the
fallback is resolved after parsing, only when the flag is absent.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
_PACKAGES = ("bench_common", "e2e_bench", "bench_parallel", "benchmark_concurrent_uploads")


class _ReachedRedisError(Exception):
    """Raised by the patched ``redis.from_url`` so the run stops where the URL is used."""


@pytest.fixture
def no_stack(monkeypatch) -> ModuleType:
    """Fresh imports of the benchmark modules with no stack, no exported port, no URL."""
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    for name in list(sys.modules):
        if name.split(".")[0] in _PACKAGES:
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.delenv("BENCHMARK_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    common = importlib.import_module("bench_common")
    nothing = SimpleNamespace(observed_port=lambda _var: "", observed_container=lambda _svc: "")
    monkeypatch.setattr(common, "_stack_target", lambda: nothing)
    return common


def _run_e2e(monkeypatch, *flags: str) -> list[str]:
    cli = importlib.import_module("e2e_bench.cli")
    used: list[str] = []

    def _from_url(url: str, **_kwargs: object) -> None:
        used.append(url)
        raise _ReachedRedisError

    monkeypatch.setattr(cli, "get_auth_token", lambda *_a, **_k: "token")
    monkeypatch.setattr(cli, "get_file_metadata", lambda *_a, **_k: {})
    monkeypatch.setattr(cli.redis, "from_url", _from_url)
    argv = ["benchmark_e2e.py", "--file-uuid", "f", "--backend-url", "http://bench", *flags]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(_ReachedRedisError):
        cli.main()
    return used


def test_an_explicit_redis_url_is_used_with_no_stack(no_stack, monkeypatch):
    assert _run_e2e(monkeypatch, "--redis-url", "redis://explicit:6390/0") == [
        "redis://explicit:6390/0"
    ]


def test_without_the_flag_the_env_url_is_used(no_stack, monkeypatch):
    monkeypatch.setenv("BENCHMARK_REDIS_URL", "redis://from-env:6391/0")
    assert _run_e2e(monkeypatch) == ["redis://from-env:6391/0"]


def test_without_the_flag_or_a_stack_the_run_refuses_when_it_needs_redis(no_stack, monkeypatch):
    cli = importlib.import_module("e2e_bench.cli")
    monkeypatch.setattr(cli, "get_auth_token", lambda *_a, **_k: "token")
    monkeypatch.setattr(cli, "get_file_metadata", lambda *_a, **_k: {})
    monkeypatch.setattr(sys, "argv", ["benchmark_e2e.py", "--file-uuid", "f"])
    with pytest.raises(SystemExit, match="no REDIS_PORT resolved"):
        cli.main()


@pytest.mark.parametrize("module", ["bench_parallel.cli", "benchmark_concurrent_uploads"])
def test_the_other_benchmarks_resolve_no_redis_url_at_import(no_stack, monkeypatch, module):
    resolved: list[str] = []

    def _record(*_args: object) -> str:
        resolved.append(module)
        return "redis://recorded:0/0"

    monkeypatch.setattr(no_stack, "load_redis_url", _record)

    importlib.import_module(module)

    assert resolved == [], f"{module} resolved the Redis URL at import"
