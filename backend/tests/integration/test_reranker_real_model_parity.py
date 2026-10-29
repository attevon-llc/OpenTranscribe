"""The thread-capped reranker scores like the uncapped one, on the REAL cross-encoder (issue #1270).

``tests/unit/test_chat_reranker_threads.py`` replaces torch and the model with recorders: it
proves WHERE ``predict`` runs and which count each thread latched. It cannot prove that the
capped path returns the scores the model would have given anyway, which is the claim Wave 3
used to check by hand with ``scripts/benchmark_rag.py`` (that tool needs a seeded corpus, a
settled index and a live LLM; this needs only the cached cross-encoder and a fixed word list).

Measured on the fixed fixture, not a corpus: the order and the scores of ``rerank`` through the
scoring pool must equal bare ``model.predict`` on the calling thread (torch's default thread
count), and a deployment that does not rerank must not touch torch's thread state at all.

Loads ``cross-encoder/ms-marco-MiniLM-L-6-v2`` from the local cache on CPU. Where the weights
are cached but cannot load (the transformers 5 / huggingface-hub 1.17 pair), this FAILS: a skip
there would hide exactly the breakage Wave 3 was checking for.
"""

from __future__ import annotations

import pytest

from app.core import constants as C  # noqa: N812
from app.scripts.benchmark_reranker_threads import _QUERY
from app.scripts.benchmark_reranker_threads import synthetic_passages
from app.services.chat import reranker as reranker_mod
from app.services.chat import reranker_threads

pytestmark = [pytest.mark.integration, pytest.mark.models]

#: Float32 CPU matmuls reassociate across thread counts; the cross-encoder logits agree to
#: well under this, while a wrong model or a mangled pair differs by whole units.
SCORE_TOLERANCE = 1e-3


class _Hit:
    def __init__(self, content: str) -> None:
        self.content = content
        self.score = 0.0
        self.language = "en"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("OMP_NUM_THREADS", "TORCH_NUM_THREADS"):
        monkeypatch.delenv(var, raising=False)
    reranker_mod.reset_reranker_state()
    yield
    reranker_mod.reset_reranker_state()


@pytest.fixture
def model():
    pytest.importorskip("sentence_transformers", reason="CPU-only CI image has no weights")
    loaded = reranker_mod.get_reranker(C.CHAT_RERANKER_MODEL)
    assert loaded is not None, (
        "the cross-encoder did not load; run scripts/download-models.py, or this is the "
        "transformers/huggingface-hub regression this test exists to catch"
    )
    return loaded


def _process_default_threads() -> int:
    import torch

    return int(reranker_threads._on_fresh_thread(torch.get_num_threads))


def _hits() -> list[_Hit]:
    return [_Hit(p) for p in synthetic_passages(12, words=60)]


def test_capped_scores_equal_uncapped_scores_and_keep_the_order(model, monkeypatch):
    monkeypatch.setitem(reranker_threads.settings.__dict__, "CHAT_RERANKER_TORCH_THREADS", "2")
    hits = _hits()
    bare = [float(s) for s in model.predict([(_QUERY, h.content) for h in hits])]

    reranked = reranker_mod.rerank(_QUERY, list(hits), max_pairs=50)

    assert len(reranked) == len(hits)
    capped = {id(h): h.score for h in reranked}
    for hit, expected in zip(hits, bare, strict=True):
        assert abs(capped[id(hit)] - expected) < SCORE_TOLERANCE
    expected_order = sorted(range(len(hits)), key=lambda i: bare[i], reverse=True)
    assert [id(h) for h in reranked] == [id(hits[i]) for i in expected_order]
    # Control: the fixture is not degenerate, so equal order is a statement about the model.
    assert len({round(s, 3) for s in bare}) > 1


def test_the_pool_really_scored_and_left_the_process_default_alone(model, monkeypatch):
    monkeypatch.setitem(reranker_threads.settings.__dict__, "CHAT_RERANKER_TORCH_THREADS", "2")
    before = _process_default_threads()

    reranker_mod.rerank(_QUERY, _hits(), max_pairs=50)

    assert reranker_threads._pool is not None, "scoring did not go through the capped pool"
    assert _process_default_threads() == before


def test_a_deployment_that_does_not_rerank_never_touches_torch_threads(model):
    """Off means off: nothing scored, so no pool and no change to torch's thread state."""
    before = _process_default_threads()

    one = _hits()[:1]
    assert reranker_mod.rerank(_QUERY, one, max_pairs=50) is one  # a single hit is not scored

    assert reranker_threads._pool is None
    assert _process_default_threads() == before
