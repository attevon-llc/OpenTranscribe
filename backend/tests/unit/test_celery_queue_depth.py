"""Tests for ``app.core.celery_metrics.queue_snapshot`` (issue #892).

Before this module there was NO test for ``celery_metrics`` at all. #892's real
shape was two separate implementations of "how deep is a Celery queue" that
disagreed: the ``/metrics`` gauge summed the 10 kombu priority sub-lists
correctly, and ``stats_helpers.get_queue_depths`` read the RESULT BACKEND
(``celery_app.backend.client``) with a bare, un-sharded ``LLEN`` against the
BROKER's key naming — undercounting on both axes, and additionally never
counting a task a worker had already picked up and not yet acknowledged (the
autoscaler inversion: a saturated GPU fleet reported queue depth 0).

Every test here uses a fake pipeline/client so it needs no live Redis — the
matching live-kombu proof is
``tests/integration/test_celery_queue_depth_live.py``. Keys are derived from
``kombu.transport.redis.Channel.sep`` / ``.unacked_key``, never a literal, so
a kombu upgrade that changes either shows up as a real assertion failure
here rather than a silent drift.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from kombu.transport.redis import Channel

from app.core.celery_metrics import _RESERVED_ATTRIBUTION_LIMIT
from app.core.celery_metrics import queue_snapshot
from app.core.constants import CeleryQueues

SEP = Channel.sep
UNACKED_KEY = Channel.unacked_key


class _FakePipeline:
    """Records every command issued, in order, and answers them at ``execute()``."""

    def __init__(self, llen_map: dict[str, int], unacked_values: list):
        self.commands: list[tuple[str, str]] = []
        self._llen_map = llen_map
        self._unacked_values = unacked_values

    def llen(self, key):
        self.commands.append(("llen", key))
        return self

    def hvals(self, key):
        self.commands.append(("hvals", key))
        return self

    def execute(self) -> list[int | list]:
        # A real redis-py pipeline's execute() is genuinely heterogeneous: each
        # command answers with its own type (LLEN -> int, HVALS -> list).
        results: list[int | list] = []
        for cmd, key in self.commands:
            if cmd == "llen":
                results.append(self._llen_map.get(key, 0))
            else:
                results.append(self._unacked_values)
        return results


class _FakeClient:
    """Stands in for the Redis broker client passed to ``queue_snapshot``."""

    def __init__(self, llen_map: dict[str, int] | None = None, unacked_values: list | None = None):
        self._llen_map = llen_map or {}
        self._unacked_values = unacked_values if unacked_values is not None else []
        self.pipelines: list[_FakePipeline] = []

    def llen(self, key):
        """A bare, unpipelined LLEN -- for baking in the pre-#892 undercount as evidence."""
        return self._llen_map.get(key, 0)

    def pipeline(self, transaction=False):
        pipe = _FakePipeline(self._llen_map, self._unacked_values)
        self.pipelines.append(pipe)
        return pipe


class _BrokenClient:
    def pipeline(self, transaction=False):
        raise RuntimeError("redis unreachable")


def test_a_task_at_a_non_default_priority_is_counted():
    """The defect: a bare LLEN against the bare queue name misses every non-default priority."""
    key = f"utility{SEP}7"
    fake = _FakeClient(llen_map={key: 3})

    snapshot = queue_snapshot(fake)

    assert snapshot["utility"]["pending"] == 3
    # The red-first evidence: this is exactly what a bare LLEN returns today.
    assert fake.llen("utility") == 0


def test_all_ten_priority_sub_lists_are_summed():
    llen_map = {"gpu": 1}
    for priority in range(1, 10):
        llen_map[f"gpu{SEP}{priority}"] = 1
    fake = _FakeClient(llen_map=llen_map)

    snapshot = queue_snapshot(fake)

    assert snapshot["gpu"]["pending"] == 10


def test_a_reserved_task_is_still_counted(monkeypatch):
    """The inversion test: today this reports 0 no matter how loaded the worker is."""
    unacked = [json.dumps([{}, "gpu", "gpu"])]
    fake = _FakeClient(unacked_values=unacked)

    snapshot = queue_snapshot(fake)

    assert snapshot["gpu"]["reserved"] == 1

    from app.utils.stats_helpers import get_queue_depths

    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)
    assert get_queue_depths()["gpu"] == 1


def test_the_snapshot_is_one_round_trip():
    fake = _FakeClient()

    queue_snapshot(fake)

    assert len(fake.pipelines) == 1
    pipe = fake.pipelines[0]
    assert len(pipe.commands) == len(CeleryQueues.ALL) * 10 + 1


def test_a_corrupt_unacked_entry_does_not_break_the_snapshot():
    unacked = [b"not-json-at-all", json.dumps([{}, "cpu", "cpu"])]
    fake = _FakeClient(unacked_values=unacked)

    snapshot = queue_snapshot(fake)

    assert snapshot["cpu"]["reserved"] == 1


def test_a_broker_error_reports_all_zero():
    snapshot = queue_snapshot(_BrokenClient())

    assert snapshot == {name: {"pending": 0, "reserved": 0} for name in CeleryQueues.ALL}


def test_reserved_attribution_is_skipped_above_the_cap():
    """Real bound here is Σ prefetch×concurrency ≈ 37 (every worker runs at the global
    prefetch=1 -- see test_celery_reliability.test_no_worker_overrides_the_global_prefetch_
    multiplier); 10_000 is a wide safety margin over that, not a tuned value.
    """
    unacked = [json.dumps([{}, "gpu", "gpu"])] * (_RESERVED_ATTRIBUTION_LIMIT + 1)
    fake = _FakeClient(unacked_values=unacked)

    snapshot = queue_snapshot(fake)

    assert snapshot["gpu"]["reserved"] == 0


# ---------------------------------------------------------------------------
# The guard that pins all four #892 sites at once (STEP 5d)
# ---------------------------------------------------------------------------

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
REPO_ROOT = BACKEND_ROOT.parent
CHEATSHEET = REPO_ROOT / "scripts" / "bulk-processing-cheatsheet.sh"

#: Each entry is (path relative to backend/, enclosing function name) -> written reason.
_LLEN_ALLOWLIST: dict[tuple[str, str | None], str] = {
    ("app/core/celery_metrics.py", "queue_snapshot"): (
        "the ONE priority-sharded implementation everyone else must call through"
    ),
}


class _LlenCallVisitor(ast.NodeVisitor):
    """Records every ``llen``/``LLEN`` call (by enclosing function) in one file."""

    def __init__(self, rel_path: str, hits: list[tuple[str, str | None, int]]):
        self._rel_path = rel_path
        self._hits = hits
        self._stack: list[str] = []

    def visit_FunctionDef(self, node):  # noqa: N802 — ast visitor naming convention
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    # noqa reason: `visit_AsyncFunctionDef` is dispatched by name from
    # ast.NodeVisitor.visit() and MUST match the AST node class name exactly
    # (mixedCase) -- no restructuring removes that requirement.
    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Call(self, node):  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            name = None
        if name and name.lower() == "llen" and len(node.args) == 1:
            enclosing = self._stack[-1] if self._stack else None
            self._hits.append((self._rel_path, enclosing, node.lineno))
        self.generic_visit(node)


def _iter_llen_calls() -> list[tuple[str, str | None, int]]:
    hits: list[tuple[str, str | None, int]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        rel = str(path.relative_to(BACKEND_ROOT))
        _LlenCallVisitor(rel, hits).visit(tree)
    return hits


def test_no_consumer_measures_a_queue_with_a_bare_llen():
    """RED on HEAD (pre-#892) in FOUR places: stats_helpers.get_queue_depths,
    celery_metrics.update_queue_depths (before it delegated to queue_snapshot),
    benchmark_timing.capture_queue_depth, and bulk-processing-cheatsheet.sh.
    This is the single test that makes the whole lane's claim checkable.
    """
    hits = _iter_llen_calls()
    assert hits, "the AST scan found zero llen() calls -- the scan itself is broken"

    unallowed = [(rel, fn, lineno) for rel, fn, lineno in hits if (rel, fn) not in _LLEN_ALLOWLIST]
    assert unallowed == [], (
        f"llen() called outside queue_snapshot: {unallowed} -- route through "
        "app.core.celery_metrics.queue_snapshot instead"
    )

    code_lines = [
        line
        for line in CHEATSHEET.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    script_text = "\n".join(code_lines)
    # Matches an actual `redis-cli ... LLEN <queue>` invocation, not this test's
    # own explanatory prose (nor the script's own comments, which discuss LLEN
    # by name while the code beside them only ever issues EVAL).
    bare_llen_calls = re.findall(r"redis-cli[^\n]*\bLLEN\b", script_text)
    assert bare_llen_calls == [], (
        f"bulk-processing-cheatsheet.sh still issues a bare LLEN: {bare_llen_calls} -- see "
        "app/core/celery_metrics.py's module docstring for the priority-sharded "
        "EVAL replacement"
    )
