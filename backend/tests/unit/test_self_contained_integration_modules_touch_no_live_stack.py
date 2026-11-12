"""Modules in the gate's parallel integration lane must not touch the live stack.

``run-integration-tests.sh`` runs ``INTEGRATION_SELF_CONTAINED`` concurrently with the live-stack
integration modules. That is only safe if the listed modules are genuinely self-contained
(throwaway containers, ``tmp_path``, subprocesses): the live lane contains counters and settings
reads (``test_fusion_strategy_switch``'s cluster-wide ``index_total``, global ``SystemSettings``
mutators) that any concurrent live write would corrupt. Nothing in the list may import a
live-service client or the app's engine/settings, use an unlisted fixture, or name the live
services. A new integration module is serial by default because it is not in the array.
"""

from __future__ import annotations

import ast

import pytest

from tests.unit.test_integration_split_is_a_partition import _BACKEND
from tests.unit.test_integration_split_is_a_partition import self_contained_modules

pytestmark = pytest.mark.unit

#: Import roots that are a live-service client, or the app plane bound to the live stack.
_FORBIDDEN_IMPORTS = (
    "opensearchpy", "opensearch", "minio", "boto3", "botocore", "redis", "celery", "requests",
    "httpx", "fastapi", "starlette", "app.api", "app.models", "app.services", "app.tasks",
    "app.core", "app.db.base", "app.db.session",
)  # fmt: skip
#: Names that mean "the live stack" wherever they appear as identifiers or attributes.
_FORBIDDEN_NAMES = frozenset(
    {"SystemSettings", "SessionLocal", "get_db", "app_engine", "TestClient", "settings"}
)
#: Fixtures a self-contained test may request: pytest built-ins and the throwaway-PG ones.
_ALLOWED_FIXTURES = frozenset(
    {"pg_container", "tmp_path", "tmp_path_factory", "monkeypatch", "capsys", "capfd",
     "request", "seeded_db", "target", "dump_format", "platform", "throwaway_pg"}
)  # fmt: skip
#: Port literals of the live stack (postgres, redis, opensearch, minio, api and their fresh offsets).
_LIVE_PORT_PREFIXES = ("localhost:55", "127.0.0.1:55", "localhost:517", "localhost:518")


def violations(source: str, local_fixtures_ok: bool = True) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    local_fixtures: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
            "fixture" in ast.unparse(d) for d in node.decorator_list
        ):
            local_fixtures.add(node.name)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if any(name == p or name.startswith(p + ".") for p in _FORBIDDEN_IMPORTS):
                found.append(f"imports {name}")
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            found.append(f"references {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            found.append(f"references .{node.attr}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(p in node.value for p in _LIVE_PORT_PREFIXES):
                found.append(f"names a live port: {node.value[:60]!r}")
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            allowed = _ALLOWED_FIXTURES | (local_fixtures if local_fixtures_ok else set())
            for arg in node.args.args:
                if arg.arg not in allowed:
                    found.append(f"{node.name} requests fixture {arg.arg}")
    return sorted(set(found))


def test_every_listed_module_touches_no_live_stack() -> None:
    modules = self_contained_modules()
    assert modules
    report = {
        m: v for m in modules if (v := violations((_BACKEND / m).read_text(encoding="utf-8")))
    }
    assert not report, f"live-stack contact in the parallel lane's modules: {report}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from opensearchpy import OpenSearch\n", "imports opensearchpy"),
        ("from app.core.config import settings\n", "imports app.core.config"),
        ("def test_x(db_session): pass\n", "test_x requests fixture db_session"),
        ("import requests\nrequests.get('http://localhost:5180/')\n", "names a live port"),
        (
            "from app.models import SystemSettings\nx = SystemSettings\n",
            "references SystemSettings",
        ),
    ],
)
def test_the_detector_fires(source: str, expected: str) -> None:
    assert any(expected in v for v in violations(source)), violations(source)


def test_the_detector_stays_quiet_on_a_clean_module() -> None:
    clean = (
        "import subprocess\nimport pytest\nfrom tests.integration import throwaway_pg\n"
        "def test_a(pg_container, tmp_path): subprocess.run(['true'])\n"
    )
    assert violations(clean) == []
