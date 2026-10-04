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
import time
from pathlib import Path

import pytest
from kombu.transport.redis import Channel

from app.core.celery_metrics import _RESERVED_ATTRIBUTION_LIMIT
from app.core.celery_metrics import queue_snapshot
from app.core.constants import CeleryQueues

SEP = Channel.sep
UNACKED_KEY = Channel.unacked_key


class _FakePipeline:
    """Records every command issued, in order, and answers them at ``execute()``."""

    def __init__(
        self, llen_map: dict[str, int], unacked_values: list, heads: dict[str, object] | None = None
    ):
        self.commands: list[tuple[str, str]] = []
        self._llen_map = llen_map
        self._heads = heads or {}
        self.lindex_indexes: list[int] = []
        # kombu's unacked hash is tag -> entry, and its index scores each tag by delivery
        # time; every entry here was "delivered" just now (a live holder).
        self._unacked = {f"tag-{i}": value for i, value in enumerate(unacked_values)}

    def llen(self, key):
        self.commands.append(("llen", key))
        return self

    def lindex(self, key, index):
        self.commands.append(("lindex", key))
        self.lindex_indexes.append(index)
        return self

    def hgetall(self, key):
        self.commands.append(("hgetall", key))
        return self

    def zrange(self, key, start, end, withscores=False):
        self.commands.append(("zrange", key))
        return self

    def execute(self) -> list[object]:
        # A real redis-py pipeline's execute() is genuinely heterogeneous: each
        # command answers with its own type (LLEN -> int, HGETALL -> dict, ZRANGE -> list).
        results: list[object] = []
        for cmd, key in self.commands:
            if cmd == "llen":
                results.append(self._llen_map.get(key, 0))
            elif cmd == "lindex":
                results.append(self._heads.get(key))
            elif cmd == "hgetall":
                results.append(dict(self._unacked))
            else:
                results.append([(tag, time.time()) for tag in self._unacked])
        return results


class _FakeClient:
    """Stands in for the Redis broker client passed to ``queue_snapshot``."""

    def __init__(
        self,
        llen_map: dict[str, int] | None = None,
        unacked_values: list | None = None,
        heads: dict[str, object] | None = None,
    ):
        self._llen_map = llen_map or {}
        self._unacked_values = unacked_values if unacked_values is not None else []
        self._heads = heads or {}
        self.pipelines: list[_FakePipeline] = []

    def llen(self, key):
        """A bare, unpipelined LLEN -- for baking in the pre-#892 undercount as evidence."""
        return self._llen_map.get(key, 0)

    def pipeline(self, transaction=False):
        pipe = _FakePipeline(self._llen_map, self._unacked_values, self._heads)
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
    # Ten LLENs and ten LINDEXes per queue, plus the unacked hash and its delivery-time index.
    assert len(pipe.commands) == len(CeleryQueues.ALL) * 20 + 2


def test_a_corrupt_unacked_entry_does_not_break_the_snapshot():
    unacked = [b"not-json-at-all", json.dumps([{}, "cpu", "cpu"])]
    fake = _FakeClient(unacked_values=unacked)

    snapshot = queue_snapshot(fake)

    assert snapshot["cpu"]["reserved"] == 1


def test_a_broker_error_reports_all_zero():
    snapshot = queue_snapshot(_BrokenClient())

    assert snapshot == {
        name: {
            "pending": 0,
            "reserved": 0,
            "orphaned": 0,
            "oldest_unacked_age": 0,
            "oldest_pending_age": 0,
        }
        for name in CeleryQueues.ALL
    }


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
# Oldest waiting message per queue (issue #1172)
# ---------------------------------------------------------------------------


def _message(published_at=None, eta=None, **headers) -> str:
    """A kombu Redis-transport list element: the message envelope as JSON."""
    if published_at is not None:
        headers["x-ot-published-at"] = published_at
    if eta is not None:
        headers["eta"] = eta
    return json.dumps(
        {
            "body": "W1tdLCB7fSwge31d",
            "content-encoding": "utf-8",
            "content-type": "application/json",
            "headers": {"task": "noop", "id": "x", **headers},
            "properties": {"body_encoding": "base64", "delivery_info": {"routing_key": "cpu"}},
        }
    )


def test_the_oldest_waiting_message_is_read_from_the_consuming_end_of_every_sub_list():
    now = time.time()
    fake = _FakeClient(
        heads={
            "cpu": _message(published_at=now - 30),
            f"cpu{SEP}9": _message(published_at=now - 600),
            f"gpu{SEP}3": _message(published_at=now - 5),
        }
    )

    snapshot = queue_snapshot(fake)

    assert snapshot["cpu"]["oldest_pending_age"] == pytest.approx(600, abs=2)
    assert snapshot["gpu"]["oldest_pending_age"] == pytest.approx(5, abs=2)
    assert snapshot["nlp"]["oldest_pending_age"] == 0
    # kombu LPUSHes and BRPOPs: the RIGHT end (-1) is the oldest. The live proof is
    # tests/integration/test_celery_queue_depth_live.py.
    assert set(fake.pipelines[0].lindex_indexes) == {-1}
    peeked = {key for cmd, key in fake.pipelines[0].commands if cmd == "lindex"}
    assert peeked == {
        name if priority == 0 else f"{name}{SEP}{priority}"
        for name in CeleryQueues.ALL
        for priority in range(10)
    }


def test_an_unstamped_corrupt_or_future_head_reads_zero_without_breaking_the_snapshot():
    now = time.time()
    fake = _FakeClient(
        llen_map={"cpu": 1, "gpu": 1, "nlp": 1, "utility": 1},
        heads={
            "cpu": _message(),  # an older producer: no stamp
            "gpu": b"not-json",
            "nlp": _message(published_at=now + 300),  # producer clock ahead
            "utility": json.dumps(["unexpected", "shape"]),
        },
        unacked_values=[json.dumps([{}, "gpu", "gpu"])],
    )

    snapshot = queue_snapshot(fake)

    for name in ("cpu", "gpu", "nlp", "utility"):
        assert snapshot[name]["oldest_pending_age"] == 0
        assert snapshot[name]["pending"] == 1
    assert snapshot["gpu"]["reserved"] == 1


def test_a_message_waiting_for_its_eta_is_aged_from_when_it_became_due():
    from datetime import UTC
    from datetime import datetime

    now = time.time()
    due = datetime.fromtimestamp(now - 20, tz=UTC).isoformat()
    not_yet = datetime.fromtimestamp(now + 60, tz=UTC).isoformat()
    fake = _FakeClient(
        heads={
            "cpu": _message(published_at=now - 300, eta=due),
            "gpu": _message(published_at=now - 300, eta=not_yet),
        }
    )

    snapshot = queue_snapshot(fake)

    assert snapshot["cpu"]["oldest_pending_age"] == pytest.approx(20, abs=2)
    assert snapshot["gpu"]["oldest_pending_age"] == 0


def test_the_oldest_waiting_age_survives_the_attribution_cap():
    now = time.time()
    unacked = [json.dumps([{}, "gpu", "gpu"])] * (_RESERVED_ATTRIBUTION_LIMIT + 1)
    fake = _FakeClient(unacked_values=unacked, heads={"gpu": _message(published_at=now - 45)})

    snapshot = queue_snapshot(fake)

    assert snapshot["gpu"]["oldest_pending_age"] == pytest.approx(45, abs=2)


def test_update_queue_depths_publishes_the_oldest_waiting_gauge(monkeypatch):
    from app.core.celery_metrics import update_queue_depths
    from app.core.metrics import celery_queue_oldest_message_age_seconds

    now = time.time()
    fake = _FakeClient(heads={f"embedding{SEP}4": _message(published_at=now - 90)})
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)

    update_queue_depths()

    gauge = celery_queue_oldest_message_age_seconds
    assert gauge.labels(queue="embedding")._value.get() == pytest.approx(90, abs=2)
    assert gauge.labels(queue="cpu")._value.get() == 0


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
