"""Characterization tests for ``app/tasks/speaker_embedding_migration.py``.

This task runs exactly once per backend process, 60 s after FastAPI startup, via
``app/main.py::_run_one_time_embedding_normalization`` (L442-513). It is invoked
**synchronously** (``normalize_speaker_embeddings_task.apply(throw=False)`` at L469,
not ``.delay()``) and gates on a ``SystemSettings`` row keyed
``embedding_normalization_done``. Because the whole point is a one-time migration,
whatever this module returns feeds directly into whether that flag gets set — so its
return-value *shape* is load-bearing for code outside this file.

What is pinned here, in order:

1. **The return-value shape of ``_run_normalize_embeddings``**, for the two cases that
   matter at the ``main.py`` call site:

   - When ``get_opensearch_client()`` is unavailable (L245-249), the function returns
     immediately with ``normalized: 0`` *and* an ``"error"`` key — no scan ever ran.
     Read against ``main.py`` L470 (``if ... result.result.get("normalized", 0) == 0:
     # All vectors already normalized — set flag``), this condition is satisfied by
     an infrastructure failure exactly as it is by a genuinely-clean index: **a
     transient OpenSearch outage during the 60s-after-boot window permanently marks
     the one-time migration "done",** even though no vector was ever inspected. The
     comment at that call site ("All vectors already normalized") is not checking for
     an ``"error"`` key before concluding that. This is a real bug, verified by
     reading ``main.py`` L442-513 directly (not merely asserted).
   - When vectors are actually normalized (``normalized > 0``), ``main.py``'s
     ``elif result and result.result:`` branch (L492-510) is reached and it *also*
     calls ``db.merge(setting)`` + ``db.commit()`` to set the flag. **Note:** the task
     brief asked this suite to verify a stronger claim — that the flag is *never* set
     when ``normalized > 0``, with the full scan re-running forever. Reading the
     current L469-511 shows both branches ("if ... == 0" and "elif ...") end by
     setting the flag; that specific claim does not reproduce against the code on
     disk today. The test below pins the shape this module hands to that call site
     either way, so a future change to either branch has a test to break.

2. ``_sample_check_normalized``: a 50-doc ``random_score`` sample that reports "looks
   normalized" makes ``_normalize_index`` skip the scroll entirely — pinned by proving
   the fake client's ``scroll``/full ``search`` path is never invoked.
3. ``_is_normalized``: the ``NORM_TOLERANCE = 0.01`` boundary. Because the comparison
   is a strict ``abs(norm - 1.0) < 0.01`` over floats, the literal boundary values
   0.99 and 1.01 do NOT round-trip through ``np.linalg.norm`` to exactly 0.01 away
   from 1.0 — both come out as "not normalized" in practice, same as 0.989 and 1.011.
4. ``_normalize_embeddings_batch``: a zero vector's norm is replaced with 1.0 to avoid
   a division by zero, but the vector itself is unchanged (stays all-zero) and no
   error is raised or flagged for it.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.tasks.speaker_embedding_migration import NORM_TOLERANCE
from app.tasks.speaker_embedding_migration import _is_normalized
from app.tasks.speaker_embedding_migration import _normalize_embeddings_batch
from app.tasks.speaker_embedding_migration import _normalize_index
from app.tasks.speaker_embedding_migration import _run_normalize_embeddings
from app.tasks.speaker_embedding_migration import _sample_check_normalized

pytestmark = pytest.mark.unit


def _hit(doc_id: str, embedding: list[float] | None) -> dict[str, Any]:
    return {"_id": doc_id, "_source": {"embedding": embedding}}


class FakeIndices:
    """Records ``exists``/``refresh`` calls the way the real client's would."""

    def __init__(self, exists: bool = True) -> None:
        self._exists = exists
        self.refresh_calls: list[str] = []

    def exists(self, index: str) -> bool:
        return self._exists

    def refresh(self, index: str) -> None:
        self.refresh_calls.append(index)


class FakeOpenSearchClient:
    """Minimal recorder standing in for the OpenSearch client.

    ``sample_hits`` answers the ``function_score``/``random_score`` sample-check
    query. ``scroll_pages`` is popped in order for the initial scan ``search`` call
    and every subsequent ``scroll`` call — an empty pop means "no more hits",
    ending the scroll loop.
    """

    def __init__(
        self,
        sample_hits: list[dict[str, Any]],
        scroll_pages: list[list[dict[str, Any]]] | None = None,
        indices_exist: bool = True,
    ) -> None:
        self.indices = FakeIndices(indices_exist)
        self.sample_hits = sample_hits
        self.scroll_pages: list[list[dict[str, Any]]] = list(scroll_pages or [])
        self.search_calls: list[dict[str, Any]] = []
        self.scroll_calls: list[str] = []
        self.bulk_calls: list[list[dict[str, Any]]] = []
        self.cleared_scrolls: list[str] = []

    def search(self, index: str, body: dict[str, Any], scroll: str | None = None) -> dict[str, Any]:
        self.search_calls.append({"index": index, "body": body, "scroll": scroll})
        if "function_score" in body.get("query", {}):
            return {"hits": {"hits": self.sample_hits}}
        # Full-scan initial page.
        hits = self.scroll_pages.pop(0) if self.scroll_pages else []
        return {"_scroll_id": "scroll-1", "hits": {"hits": hits}}

    def scroll(self, scroll_id: str, scroll: str) -> dict[str, Any]:
        self.scroll_calls.append(scroll_id)
        hits = self.scroll_pages.pop(0) if self.scroll_pages else []
        return {"_scroll_id": scroll_id, "hits": {"hits": hits}}

    def clear_scroll(self, scroll_id: str) -> None:
        self.cleared_scrolls.append(scroll_id)

    def bulk(self, body: list[dict[str, Any]], refresh: bool = False) -> dict[str, Any]:
        self.bulk_calls.append(body)
        return {"errors": False}


# ---------------------------------------------------------------------------
# 1. Return-value shape of _run_normalize_embeddings (main.py call-site contract)
# ---------------------------------------------------------------------------


def test_return_shape_when_opensearch_client_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client => early return with normalized=0 AND an "error" key, no scan run.

    Per main.py L470, ``result.result.get("normalized", 0) == 0`` is exactly the
    condition this shape satisfies — the call site cannot tell "infra unavailable"
    apart from "genuinely nothing to normalize" without also checking for "error".
    """
    monkeypatch.setattr("app.tasks.speaker_embedding_migration.get_opensearch_client", lambda: None)

    summary = _run_normalize_embeddings(batch_size=500)

    assert summary["normalized"] == 0
    assert summary["error"] == "OpenSearch client not available"
    # This is the exact shape main.py's L470 branch treats as "all already normalized".
    assert summary.get("normalized", 0) == 0


def test_return_shape_when_vectors_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that actually normalizes vectors reports normalized > 0 with full counts."""
    unnormalized = [10.0, 0.0, 0.0]  # norm 10, needs normalizing
    already_ok = [1.0, 0.0, 0.0]  # norm 1, already normalized

    fake_client = FakeOpenSearchClient(
        sample_hits=[_hit("sample-1", unnormalized)],  # sample fails -> triggers full scan
        scroll_pages=[
            [_hit("doc-1", unnormalized), _hit("doc-2", already_ok)],
            [],  # scroll exhausted
        ],
    )
    monkeypatch.setattr(
        "app.tasks.speaker_embedding_migration.get_opensearch_client", lambda: fake_client
    )
    monkeypatch.setattr(
        "app.tasks.speaker_embedding_migration.get_speaker_index", lambda: "speakers_test"
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.get_active_speaker_index", lambda: "speakers_test"
    )

    summary = _run_normalize_embeddings(batch_size=500)

    assert summary["total_found"] == 2
    assert summary["already_normalized"] == 1
    assert summary["normalized"] == 1
    assert summary["failed"] == 0
    assert set(summary.keys()) >= {
        "total_found",
        "already_normalized",
        "normalized",
        "failed",
        "batches_processed",
    }
    # This is the shape main.py's L492 "elif" branch reads to log + set the flag.
    assert summary.get("normalized", 0) > 0


# ---------------------------------------------------------------------------
# 2. _sample_check_normalized short-circuits the full scan
# ---------------------------------------------------------------------------


def test_sample_check_normalized_true_for_all_normalized_sample() -> None:
    client = FakeOpenSearchClient(sample_hits=[_hit("a", [1.0, 0.0]), _hit("b", [0.0, 1.0])])
    assert _sample_check_normalized(client, "speakers_test") is True


def test_sample_check_normalized_false_when_one_sample_hit_is_unnormalized() -> None:
    client = FakeOpenSearchClient(sample_hits=[_hit("a", [1.0, 0.0]), _hit("b", [5.0, 0.0])])
    assert _sample_check_normalized(client, "speakers_test") is False


def test_full_scan_skipped_when_sample_check_passes() -> None:
    """A sample that "looks normalized" must prevent the scroll from ever running."""
    fake_client = FakeOpenSearchClient(
        sample_hits=[_hit("sample-1", [1.0, 0.0])],
        # If the scroll path ran, it would consume this page — it must not.
        scroll_pages=[[_hit("doc-1", [5.0, 0.0])]],
    )
    summary: dict[str, Any] = {
        "total_found": 0,
        "already_normalized": 0,
        "normalized": 0,
        "failed": 0,
        "batches_processed": 0,
    }

    _normalize_index(fake_client, "speakers_test", batch_size=500, summary=summary)

    assert fake_client.scroll_calls == []
    assert summary["batches_processed"] == 0
    assert summary["total_found"] == 0
    # Only the sample-check search happened — the full-scan search (scroll=...) did not.
    assert all(call["scroll"] is None for call in fake_client.search_calls)
    assert len(fake_client.search_calls) == 1


def test_full_scan_runs_when_sample_check_fails() -> None:
    """Contrast case: an unnormalized sample DOES trigger the scroll."""
    fake_client = FakeOpenSearchClient(
        sample_hits=[_hit("sample-1", [5.0, 0.0])],
        scroll_pages=[[_hit("doc-1", [5.0, 0.0])], []],
    )
    summary: dict[str, Any] = {
        "total_found": 0,
        "already_normalized": 0,
        "normalized": 0,
        "failed": 0,
        "batches_processed": 0,
    }

    _normalize_index(fake_client, "speakers_test", batch_size=500, summary=summary)

    assert summary["total_found"] == 1
    assert summary["normalized"] == 1
    assert any(call["scroll"] is not None for call in fake_client.search_calls)


# ---------------------------------------------------------------------------
# 3. _is_normalized boundary at NORM_TOLERANCE = 0.01
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "norm,expected",
    [
        (0.989, False),  # clearly outside tolerance
        (0.99, False),  # nominal boundary — float rounding lands it just outside
        (1.0, True),  # exact
        (1.01, False),  # nominal boundary — float rounding lands it just outside
        (1.011, False),  # clearly outside tolerance
    ],
)
def test_is_normalized_boundary(norm: float, expected: bool) -> None:
    assert NORM_TOLERANCE == 0.01
    assert _is_normalized([norm]) is expected


def test_is_normalized_true_comfortably_inside_tolerance() -> None:
    # Contrast case proving the tolerance band is not empty.
    assert _is_normalized([0.995]) is True
    assert _is_normalized([1.005]) is True


# ---------------------------------------------------------------------------
# 4. _normalize_embeddings_batch leaves zero vectors unchanged, unflagged
# ---------------------------------------------------------------------------


def test_normalize_embeddings_batch_leaves_zero_vector_unchanged() -> None:
    batch = [
        [3.0, 4.0, 0.0],  # norm 5 -> normalizes
        [0.0, 0.0, 0.0],  # zero vector -> division-by-zero guard
        [0.0, 0.0, 5.0],  # norm 5 -> normalizes
    ]

    result = _normalize_embeddings_batch(batch)

    assert len(result) == 3
    assert result[0] == pytest.approx([0.6, 0.8, 0.0])
    # The zero vector passes through unchanged — not flagged, not raising.
    assert result[1] == pytest.approx([0.0, 0.0, 0.0])
    assert result[2] == pytest.approx([0.0, 0.0, 1.0])
    # It is a plain float list like every other entry — no error marker of any kind.
    assert all(isinstance(v, float) for v in result[1])


def test_normalize_embeddings_batch_all_zero_batch_has_no_errors() -> None:
    result = _normalize_embeddings_batch([[0.0, 0.0], [0.0, 0.0]])
    assert len(result) == 2
    for vec in result:
        assert vec == pytest.approx([0.0, 0.0])


def test_normalize_embeddings_batch_empty_input_returns_empty_list() -> None:
    assert _normalize_embeddings_batch([]) == []
