"""What the health check covers, what it repairs, and what it refuses to repair.

Issue #540. Three separate defects live here, and only the first is what the issue
was filed about:

1. **``transcript_chunks`` was never checked.** ``check_and_repair_indices`` — the one
   self-heal in the codebase — iterated ``[speakers, transcripts, speakers_v4]``. The
   legacy ``transcripts`` index (383 docs, BM25-only) was covered; the live RAG index
   was not, at all.
2. **Vector-backed indices were verified with ``match_all``.** Both post-repair checks
   inside ``_repair_index`` used it too, so a speaker index whose vector plane was
   broken would be reported "repaired via close/reopen" and never escalate.
3. **Repairing the chunk plane is destructive** — delete plus a re-embed of every
   owner's corpus — so an unattended rebuild is right for a relaxed deployment and
   wrong for a hardened one, which must surface the fault instead.

The alert's *clearing* half is tested here deliberately. A one-shot flag that never
re-arms reports the first outage and silently swallows every one after it, which is
the same "state assumed to be truth" failure as the issue itself.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from opensearchpy.exceptions import TransportError

BACKEND = Path(__file__).resolve().parents[2]
MAIN_PY = BACKEND / "app" / "main.py"

ANN_METHOD = {
    "engine": "lucene",
    "space_type": "cosinesimil",
    "name": "hnsw",
    "parameters": {"ef_construction": 256, "m": 16},
}


class _RecordingIndices:
    def __init__(self, parent: _RecordingOpenSearch) -> None:
        self._parent = parent

    def exists(self, index: str) -> bool:
        self._parent.existence_checked.append(index)
        return index in self._parent.mappings

    def exists_alias(self, name: str) -> bool:
        return False

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": {"properties": self._parent.mappings.get(index, {})}}}

    def close(self, index: str) -> None:
        self._parent.closed.append(index)

    def open(self, index: str) -> None:
        self._parent.opened.append(index)

    def forcemerge(self, index: str, max_num_segments: int) -> None:  # noqa: ARG002
        self._parent.force_merged.append(index)

    def delete(self, index: str, **kwargs: Any) -> None:  # noqa: ARG002
        self._parent.deleted.append(index)


class _RecordingOpenSearch:
    """Records which indices were probed, and how."""

    def __init__(
        self,
        mappings: dict[str, dict[str, Any]],
        *,
        search_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.mappings = mappings
        self.search_errors = search_errors or {}
        self.existence_checked: list[str] = []
        self.knn_searched: list[str] = []
        self.match_all_searched: list[str] = []
        self.closed: list[str] = []
        self.opened: list[str] = []
        self.force_merged: list[str] = []
        self.deleted: list[str] = []
        self.indices = _RecordingIndices(self)

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        if "knn" in body.get("query", {}):
            self.knn_searched.append(index)
        else:
            self.match_all_searched.append(index)
        error = self.search_errors.get(index)
        if error is not None:
            raise error
        return {"hits": {"total": {"value": 1, "relation": "eq"}, "hits": []}}

    def count(self, index: str) -> dict[str, int]:  # noqa: ARG002
        return {"count": 5}


def _ann(dimension: int = 384) -> dict[str, Any]:
    return {"embedding": {"type": "knn_vector", "dimension": dimension, "method": ANN_METHOD}}


def _non_ann() -> dict[str, Any]:
    return {"embedding": {"type": "knn_vector", "dimension": 384, "doc_values": True}}


@pytest.fixture
def healthy_cluster(monkeypatch) -> _RecordingOpenSearch:
    """A cluster where every index answers, wired into both modules that read it."""
    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import repair  # noqa: F401
    from app.services.search import index_health as health

    fake = _RecordingOpenSearch(
        {
            "speakers": _ann(256),
            "speakers_v4": _ann(256),
            "transcripts": _non_ann(),
            "transcript_chunks": _ann(384),
        }
    )
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    monkeypatch.setattr(health, "clear_corruption_notice", lambda _index: None)
    os_client.reset_knn_health_cache()
    return fake


# ---------------------------------------------------------------------------
# Coverage: the index the issue is about
# ---------------------------------------------------------------------------
def test_the_chunks_index_is_health_checked(healthy_cluster):
    """It was covered by NOTHING before #540.

    ``check_and_repair_indices`` iterated ``[speakers, transcripts, speakers_v4]``,
    so the legacy BM25-only index was checked and the live RAG index was not.
    """
    from app.services.search.index_health import check_and_repair_chunks_index

    check_and_repair_chunks_index()

    assert "transcript_chunks" in healthy_cluster.knn_searched


def test_the_chunk_plane_is_probed_with_knn_and_bm25_indices_with_match_all(healthy_cluster):
    """The probe is chosen by what the index can serve, not applied blindly.

    ``transcripts`` declares knn_vector with **no ANN method**, so ANN-probing it is
    rejected 400 with a message containing ``search_phase_execution_exception`` —
    which reads as corruption. It must take the match_all path.
    """
    from app.services.opensearch_service.repair import check_and_repair_indices
    from app.services.search.index_health import check_and_repair_chunks_index

    check_and_repair_indices()
    check_and_repair_chunks_index()

    assert "transcript_chunks" in healthy_cluster.knn_searched
    assert "speakers_v4" in healthy_cluster.knn_searched
    assert "transcripts" in healthy_cluster.match_all_searched
    assert "transcripts" not in healthy_cluster.knn_searched


def test_a_healthy_cluster_repairs_nothing(healthy_cluster):
    """The expensive direction: a false positive deletes and re-embeds a corpus."""
    from app.services.opensearch_service.repair import check_and_repair_indices
    from app.services.search.index_health import check_and_repair_chunks_index

    assert check_and_repair_indices() == []
    assert check_and_repair_chunks_index() == []
    assert healthy_cluster.deleted == []
    assert healthy_cluster.closed == []


# ---------------------------------------------------------------------------
# Layering: the repair must not import back down into the search package
# ---------------------------------------------------------------------------
def test_opensearch_service_forms_no_import_cycle_with_the_search_package():
    """One import edge silently deleted type checking across 66 call sites.

    ``search.indexing_service`` imports ``opensearch_service``. Putting the chunk
    rebuild in ``opensearch_service/repair.py`` meant importing ``indexing_service``
    back the other way — a package **cycle**. It runs fine (the import sits inside a
    function) and mypy resolves the cycle by degrading the module to ``Any``, so
    every ``opensearch_client.indices.…`` access in ``indexing_service`` and
    ``reindex_task`` stopped being checked.

    MEASURED, not assumed: with a clean mypy cache, adding a single function-level
    ``from app.services.search.indexing_service import CHUNKS_ALIAS_NAME`` to
    ``repair.py`` produces **66 errors in 5 files**; removing it produces zero.

    The rule is "no cycle", not "no edge": ``client.py`` already imports
    ``search.tenant_scope``, which is a leaf and imports nothing back, so that edge
    is harmless. This test resolves each target and flags it only if it imports
    ``opensearch_service`` in return — otherwise it would forbid a legal import and
    invite a suppression.

    Asserted over source text because the imports live inside function bodies, where
    an import-linter contract keyed on module-level imports would not see them.
    """
    services = BACKEND / "app" / "services"
    edge_re = re.compile(r"from\s+app\.services\.search\.([A-Za-z0-9_]+)\s+import")

    def imports_opensearch_service(module: str) -> bool:
        target = services / "search" / f"{module}.py"
        if not target.exists():
            return False
        return "app.services.opensearch_service" in target.read_text()

    offenders: list[str] = []
    for path in sorted((services / "opensearch_service").glob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            match = edge_re.search(line.strip())
            if match and imports_opensearch_service(match.group(1)):
                offenders.append(f"{path.name}:{i}: {line.strip()}")

    assert not offenders, (
        "These imports form a CYCLE: opensearch_service is the layer below "
        "services/search, and each target imports opensearch_service back. mypy "
        "resolves a cycle by treating the module as Any, which silently stops "
        "checking every call site that uses it:\n  " + "\n  ".join(offenders)
    )


def test_the_cycle_guard_would_notice_a_real_cycle():
    """Guard the guard: the detector must fire on the edge that actually broke this.

    Without this, a detector that quietly matches nothing reports a clean tree and
    the next person reintroduces the import.
    """
    services = BACKEND / "app" / "services"
    edge_re = re.compile(r"from\s+app\.services\.search\.([A-Za-z0-9_]+)\s+import")

    match = edge_re.search("    from app.services.search.indexing_service import CHUNKS_ALIAS_NAME")
    assert match is not None and match.group(1) == "indexing_service"
    assert (
        "app.services.opensearch_service"
        in (services / "search" / "indexing_service.py").read_text()
    ), "indexing_service must still import opensearch_service, or the guard is moot"

    # ...and stay clean for the leaf edge that is legitimately present today.
    leaf = edge_re.search("    from app.services.search.tenant_scope import org_filter_clauses")
    assert leaf is not None and leaf.group(1) == "tenant_scope"
    assert (
        "app.services.opensearch_service"
        not in (services / "search" / "tenant_scope.py").read_text()
    ), "tenant_scope is a leaf; if that changes, client.py's import becomes a cycle"


# ---------------------------------------------------------------------------
# Repair vs surface
# ---------------------------------------------------------------------------
@pytest.fixture
def corrupt_chunks(monkeypatch) -> _RecordingOpenSearch:
    """A cluster where only the chunk plane\'s vector segments are broken."""
    from app.services.opensearch_service import client as os_client

    fake = _RecordingOpenSearch(
        {"transcript_chunks": _ann(384)},
        search_errors={
            "transcript_chunks": TransportError(
                503, "search_phase_execution_exception", {"failed_shards": []}
            )
        },
    )
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    os_client.reset_knn_health_cache()
    return fake


def test_a_hardened_deployment_surfaces_corruption_instead_of_rebuilding(
    corrupt_chunks, monkeypatch
):
    """Deleting and re-embedding every owner\'s corpus is an operator\'s decision."""
    from app.core.config import settings
    from app.services.search import index_health as health

    monkeypatch.setattr(type(settings), "is_hardened", property(lambda _self: True))
    monkeypatch.setattr(health, "clear_corruption_notice", lambda _index: None)

    notified: list[tuple[str, Any]] = []
    monkeypatch.setattr(health, "notify_corruption", lambda i, p: notified.append((i, p)))

    rebuilt: list[bool] = []

    def _record_rebuild() -> bool:
        rebuilt.append(True)
        return True

    monkeypatch.setattr(health, "rebuild_chunks_index", _record_rebuild)

    repaired = health.check_and_repair_chunks_index()

    assert repaired == [], "a hardened deployment must not self-heal"
    assert rebuilt == [], "the destructive rebuild must not have been attempted"
    assert corrupt_chunks.deleted == [], "the index must survive"
    assert [index for index, _probe in notified] == ["transcript_chunks"]


def test_a_relaxed_deployment_self_heals(corrupt_chunks, monkeypatch):
    """Chat answering ungrounded on every turn is worse than an unattended rebuild."""
    from app.core.config import settings
    from app.services.search import index_health as health

    monkeypatch.setattr(type(settings), "is_hardened", property(lambda _self: False))
    monkeypatch.setattr(health, "clear_corruption_notice", lambda _index: None)

    notified: list[str] = []
    monkeypatch.setattr(health, "notify_corruption", lambda i, _p: notified.append(i))

    rebuilt: list[bool] = []

    def _record_rebuild() -> bool:
        rebuilt.append(True)
        return True

    monkeypatch.setattr(health, "rebuild_chunks_index", _record_rebuild)

    repaired = health.check_and_repair_chunks_index()

    assert repaired == ["transcript_chunks"]
    assert rebuilt == [True], "the rebuild must actually be attempted"
    assert notified == [], "a self-healing deployment does not page an admin"


def test_an_inconclusive_probe_neither_repairs_nor_alerts(monkeypatch):
    """``absent`` / ``unknown`` are not evidence of corruption.

    Repair is destructive, so it keys off a positive ``corrupt`` verdict rather than
    off "not serviceable" — which would also fire for an index that is merely missing.
    """
    from app.services.opensearch_service import client as os_client
    from app.services.search import index_health as health

    monkeypatch.setattr(os_client, "opensearch_client", _RecordingOpenSearch({}))
    os_client.reset_knn_health_cache()

    rebuilt: list[bool] = []
    notified: list[str] = []

    def _record_rebuild() -> bool:
        rebuilt.append(True)
        return True

    monkeypatch.setattr(health, "rebuild_chunks_index", _record_rebuild)
    monkeypatch.setattr(health, "notify_corruption", lambda i, _p: notified.append(i))

    assert health.check_and_repair_chunks_index() == []
    assert rebuilt == []
    assert notified == []


def test_the_close_reopen_strategy_runs_before_any_destructive_step(corrupt_chunks):
    """Non-destructive strategies must be exhausted first."""
    from app.services.opensearch_service import repair

    repair._repair_index("transcript_chunks")

    assert corrupt_chunks.closed == ["transcript_chunks"]
    assert corrupt_chunks.opened == ["transcript_chunks"]
    assert corrupt_chunks.force_merged == ["transcript_chunks"]


def test_close_reopen_is_verified_with_a_knn_query_not_match_all(corrupt_chunks):
    """Otherwise a still-broken vector plane reports itself repaired.

    ``_repair_index`` used ``match_all`` to confirm its own fix, so for the exact
    failure this issue describes — BM25 fine, kNN 503ing — close/reopen would return
    True and the real repair would never run.
    """
    from app.services.opensearch_service import repair

    repair._repair_index("transcript_chunks")

    assert "transcript_chunks" in corrupt_chunks.knn_searched
    assert corrupt_chunks.match_all_searched == []


# ---------------------------------------------------------------------------
# The alert must re-arm
# ---------------------------------------------------------------------------
def test_the_corruption_notice_is_sent_once_then_cleared_on_recovery(db_session, monkeypatch):
    """A latched flag reports the first outage and swallows every one after it."""
    from app.services import system_settings_service as sss
    from app.services.search import index_health as health

    key = health.corruption_notice_key("transcript_chunks")
    sent: list[str] = []

    class _Scope:
        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return False

    # `index_health` imports SessionLocal inside the function body, so the patch has
    # to land on the module it is imported FROM.
    monkeypatch.setattr("app.db.base.SessionLocal", _Scope)
    monkeypatch.setattr(
        "app.services.backup_alerts._notify_admins",
        lambda db, **kw: sent.append(kw["message"]),
    )

    probe = type("P", (), {"detail": "503 search_phase_execution_exception"})()

    health.notify_corruption("transcript_chunks", probe)
    assert len(sent) == 1, "the first outage must page admins"
    assert sss.get_setting_bool(db_session, key, False) is True

    health.notify_corruption("transcript_chunks", probe)
    assert len(sent) == 1, "a still-broken index must not re-page every tick"

    health.clear_corruption_notice("transcript_chunks")
    assert sss.get_setting_bool(db_session, key, False) is False

    health.notify_corruption("transcript_chunks", probe)
    assert len(sent) == 2, "a NEW outage after recovery must page again"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def test_the_startup_maintenance_dispatch_is_elected(monkeypatch):
    """Two overlapping passes corrupted three reindexes in one day.

    In dev this also fires on every ``app/**.py`` save, because uvicorn --reload
    re-runs the lifespan startup.
    """
    import asyncio

    from app import main as app_main
    from app.tasks import search_maintenance_task as smt

    dispatched: list[str] = []
    monkeypatch.setattr(smt.search_index_maintenance_task, "delay", lambda: dispatched.append("x"))

    # Bind the real sleep before patching, or the replacement calls itself.
    real_sleep = asyncio.sleep

    async def _no_delay(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _no_delay)

    monkeypatch.setattr("app.utils.boot_once.run_once_per_boot", lambda _step: False)
    asyncio.run(app_main._run_search_maintenance())
    assert dispatched == [], "a replica that lost the election must not dispatch"

    monkeypatch.setattr("app.utils.boot_once.run_once_per_boot", lambda _step: True)
    asyncio.run(app_main._run_search_maintenance())
    assert len(dispatched) == 1, "the election winner must dispatch"


def test_the_maintenance_task_declines_when_the_lock_is_held(monkeypatch):
    """``with_task_lock`` replaces a hand-rolled SET NX that let Redis errors escape."""
    from app.tasks import search_maintenance_task as smt
    from app.utils import task_lock

    ran: list[bool] = []

    def _record_run() -> dict[str, bool]:
        ran.append(True)
        return {"ok": True}

    monkeypatch.setattr(smt, "_run_search_maintenance", _record_run)

    from contextlib import contextmanager

    @contextmanager
    def _busy(lock_key: str, timeout: int = 300, blocking_timeout: int = 0):  # noqa: ARG001
        yield False

    monkeypatch.setattr(task_lock.task_lock_manager, "acquire_lock", _busy)

    result = smt.search_index_maintenance_task()

    assert result["skipped"] is True
    assert ran == [], "the body must not run while another pass holds the lock"


def test_the_maintenance_lock_key_is_cleared_on_startup():
    """The stale-key sweep must name the key the task actually takes.

    Renaming the lock without renaming this entry leaves a dead pattern behind and
    a real orphaned lock uncleared for its full TTL.
    """
    from app.tasks.search_maintenance_task import MAINTENANCE_LOCK_KEY

    source = MAIN_PY.read_text()
    assert f'"{MAINTENANCE_LOCK_KEY}"' in source
    assert '"search_maintenance_lock"' not in source, "the old key name must be gone"


def test_the_structural_index_calls_are_taken_under_a_lock():
    """``reindex_lock`` is per-USER; the chunks index is shared.

    Both guarded calls can DELETE the index, so two owners' coordinators starting
    together could have one delete the other's freshly created index. Asserted over
    the AST so the guard cannot be silently unwrapped.
    """
    source = (BACKEND / "app" / "tasks" / "reindex_task.py").read_text()
    tree = ast.parse(source)

    guarded: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if not any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", "") == "_index_structure_lock"
            for item in node.items
        ):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                name = getattr(inner.func, "id", "") or getattr(inner.func, "attr", "")
                guarded.add(name)

    assert "_check_and_recreate_stale_index" in guarded
    assert "recreate_index_for_dimension" in guarded


def test_the_structure_lock_is_scoped_to_the_index_name():
    """A lock keyed on anything coarser serialises unrelated work."""
    from app.core.config import settings
    from app.tasks import reindex_task

    captured: list[str] = []

    class _Manager:
        def acquire_lock(self, lock_key: str, **kwargs: Any):  # noqa: ARG002
            captured.append(lock_key)
            from contextlib import nullcontext

            return nullcontext(True)

    import app.utils.task_lock as tl

    original = tl.task_lock_manager
    tl.task_lock_manager = _Manager()  # type: ignore[assignment]
    try:
        with reindex_task._index_structure_lock():
            pass
    finally:
        tl.task_lock_manager = original

    assert captured == [f"opensearch_index_structure_lock:{settings.OPENSEARCH_CHUNKS_INDEX}"]
