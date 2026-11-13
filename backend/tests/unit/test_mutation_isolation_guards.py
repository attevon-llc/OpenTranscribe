"""Provisioning may only drop ot_mut_* databases, only on otfresh-* stacks, never FLUSHALL."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib"
# every docker exec argv and every redis command, in order
CALLS: list[tuple] = []


@pytest.fixture
def iso(monkeypatch):
    monkeypatch.syspath_prepend(str(_LIB))
    sys.modules.pop("mutation_isolation", None)
    mod = importlib.import_module("mutation_isolation")
    CALLS.clear()

    def fake_exec(name, *cmd):
        CALLS.append(("exec", name, cmd))
        script = " ".join(cmd)
        if "SELECT version_num" in script:
            return 0, "HEADREV\n"
        return 0, ""

    class FakeRedis:
        def __init__(self, index):
            self.index = index

        def flushdb(self):
            CALLS.append(("flushdb", self.index))

        def config_get(self, _):
            return {"databases": "16"}

    monkeypatch.setattr(mod, "container_exec", fake_exec)
    monkeypatch.setattr(mod, "_redis_client", lambda index: FakeRedis(index))
    monkeypatch.setattr(mod, "_head", lambda stack: "HEADREV")
    return mod


def _stack(iso, monkeypatch, project):
    stack = SimpleNamespace(project=project, first=lambda svc: f"{project}-{svc}")
    monkeypatch.setattr(iso, "_stack", lambda: stack)


@pytest.mark.parametrize("project", ["transcribe-app", "opentranscribe", "otfresh"])
def test_provision_refuses_a_live_or_stock_project_and_sends_nothing(iso, monkeypatch, project):
    _stack(iso, monkeypatch, project)
    with pytest.raises(iso.StackError) as err:
        iso.provision(["spans", "session"])
    assert err.value.code == 21
    assert CALLS == []


def test_provision_on_a_fresh_stack_migrates_the_template_and_flushes_only_module_indexes(
    iso, monkeypatch
):
    _stack(iso, monkeypatch, "otfresh-mut")
    iso.provision(["spans", "session"])
    flushed = [c[1] for c in CALLS if c[0] == "flushdb"]
    assert flushed == [2, 3]
    flat = " | ".join(" ".join(c[2]) for c in CALLS if c[0] == "exec")
    assert "FLUSHALL" not in flat
    assert "POSTGRES_DB=ot_mut_template python -m app.startup.migrate" in flat
    assert "CREATE DATABASE ot_mut_spans TEMPLATE ot_mut_template" in flat
    drops = [part for part in flat.split("DROP DATABASE IF EXISTS ")[1:]]
    assert drops and all(d.startswith("ot_mut_") for d in drops)


def test_a_drop_outside_the_prefix_is_refused_before_any_sql(iso):
    for name in ("postgres", "opentranscribe", "ot_mut_", "OT_MUT_x", "ot_mut_a;b"):
        with pytest.raises(iso.IsolationError):
            iso._drop_create("pg", name)
    assert CALLS == []


@pytest.mark.parametrize("index", [0, 1, -1])
def test_flushdb_is_refused_for_the_stacks_own_indexes(iso, index):
    with pytest.raises(iso.IsolationError):
        iso._flush(index)
    assert CALLS == []


def test_a_template_that_did_not_reach_head_fails_loudly(iso, monkeypatch):
    _stack(iso, monkeypatch, "otfresh-mut")
    monkeypatch.setattr(iso, "_head", lambda stack: "OTHERHEAD")
    with pytest.raises(iso.IsolationError, match="alembic head"):
        iso.provision(["spans", "session"])
