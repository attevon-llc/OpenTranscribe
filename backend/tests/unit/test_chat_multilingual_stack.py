"""RAG support is derived from the configured stack, not hardcoded to English (#453).

Two defects, one theme: the chat stack asserted "English only" as a fact about the
software when it is really a fact about **this deployment's configuration**.

1. ``SUPPORTED_RAG_LANGUAGES`` was a hardcoded ``frozenset({"en"})``. An operator who
   had deliberately selected a multilingual embedding model — and whose deployment was
   therefore genuinely serving Spanish — still got "Spanish is unsupported" on every
   single turn. A constant cannot know what was configured.

2. The cross-encoder reranker is ``ms-marco-MiniLM-L-6-v2``, English MS MARCO, and it
   ran over every pool regardless of language. That is not a missed optimisation:
   ``rerank`` **overwrites** ``hit.score`` with the cross-encoder's output, so a
   correct retrieval order over Spanish chunks is replaced by the ordering of a model
   that cannot read them.

⚠️ **Fixing these does not make BM25 multilingual**, and neither test pretends it
does. The ``transcript_chunks`` analyzer is still ``english_stop`` +
``english_snowball``. What is fixed is that the system stops making a claim it cannot
support in either direction — it no longer calls a configured multilingual deployment
unsupported, and it no longer lets an English-only model reorder text it cannot read.

⚠️ **Both fail CLOSED to English.** An unreadable model setting, or a pool whose
languages were never detected, must land on the conservative answer: a warning that
wrongly appears is a nuisance, a warning that wrongly vanishes hides the silent
failure it exists to surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.services.chat import language as lang_mod
from app.services.chat.language import SUPPORTED_RAG_LANGUAGES
from app.services.chat.language import _classify
from app.services.chat.language import supported_rag_languages


class _StubSession:
    """Stands in for a Session. Never queried — the lookup it drives is patched."""


def _session() -> Session:
    """A deliberate test double. ``supported_rag_languages`` only branches on whether
    a session was supplied at all; it never touches it, because the model selection is
    read through ``settings_service``. The cast records that this is a stand-in rather
    than widening the production signature to accept anything."""
    return cast("Session", _StubSession())


def _use_model(monkeypatch: pytest.MonkeyPatch, language_type: str) -> None:
    """Point the resolver at a registry entry of the given kind."""
    monkeypatch.setattr(
        "app.services.search.settings_service.get_search_embedding_model",
        lambda: "stub-model",
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.constants.OPENSEARCH_EMBEDDING_MODELS",
        {"stub-model": {"language_type": language_type, "languages": ["multilingual"]}},
        raising=False,
    )


# ---------------------------------------------------------------------------
# 1. Support is derived from the active embedding model
# ---------------------------------------------------------------------------
def test_a_multilingual_model_reports_open_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect: a correctly-configured multilingual deployment was told it wasn't."""
    _use_model(monkeypatch, "multilingual")

    assert supported_rag_languages(_session()) is lang_mod.ALL_LANGUAGES


def test_an_english_model_still_reports_english_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the default deployment's behaviour must not move."""
    _use_model(monkeypatch, "english")

    assert supported_rag_languages(_session()) == SUPPORTED_RAG_LANGUAGES


def test_an_unresolvable_model_fails_closed_to_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken settings read must not silence the warning.

    Silence would be indistinguishable from "this deployment is multilingual", which
    is the one wrong answer that hides a real failure instead of annoying someone.
    """

    def _boom() -> str:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(
        "app.services.search.settings_service.get_search_embedding_model",
        _boom,
        raising=False,
    )

    assert supported_rag_languages(_session()) == SUPPORTED_RAG_LANGUAGES


def test_no_session_means_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers with no session cannot know the configuration; they get the safe answer."""
    _use_model(monkeypatch, "multilingual")

    assert supported_rag_languages(None) == SUPPORTED_RAG_LANGUAGES


def test_open_support_makes_no_language_unsupported() -> None:
    """The derivation has to actually reach the classifier, or nothing changes."""
    open_scope = _classify(["es", "ja", "en", None], lang_mod.ALL_LANGUAGES)

    assert open_scope.unsupported == (), (
        f"a multilingual deployment still reported unsupported languages: {open_scope.unsupported}"
    )
    assert open_scope.has_unsupported is False
    assert open_scope.unknown_files == 1, "an undetected language is still its own bucket"


def test_english_only_support_still_flags_the_others() -> None:
    """The control for the above: the warning must survive on an English deployment."""
    closed = _classify(["es", "ja", "en", None], SUPPORTED_RAG_LANGUAGES)

    assert closed.unsupported == ("es", "ja")
    assert closed.supported == ("en",)


# ---------------------------------------------------------------------------
# 2. The English reranker is skipped on non-English pools
# ---------------------------------------------------------------------------
def _hit(language: str, score: float = 1.0):
    return SimpleNamespace(language=language, content="algo sobre el presupuesto", score=score)


def test_a_non_english_pool_keeps_its_retrieval_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect: an English cross-encoder overwrote scores it could not compute.

    The stub model would return a reversing score if it ran, so a passthrough result
    is unambiguous evidence that it did not.
    """
    from app.services.chat import reranker

    monkeypatch.setattr(
        reranker, "get_reranker", lambda: pytest.fail("the English reranker was invoked")
    )
    hits = [_hit("es"), _hit("es"), _hit("en")]

    assert reranker.rerank("presupuesto", hits) is hits


def test_an_english_pool_is_still_reranked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: without it, "always skip" would pass every assertion above."""
    from app.services.chat import reranker

    called: list[int] = []

    class _Model:
        def predict(self, pairs):
            called.append(len(pairs))
            return [0.1, 0.9]

    monkeypatch.setattr(reranker, "get_reranker", lambda: _Model())
    hits = [_hit("en"), _hit("en")]

    result = reranker.rerank("budget", hits)

    assert called == [2], "the reranker was skipped on an all-English pool"
    assert [h.score for h in result] == [0.9, 0.1], "the pool was not reordered"


def test_an_all_unknown_pool_is_still_reranked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown is not a vote. A library recorded before detection must not lose reranking."""
    from app.services.chat import reranker

    called: list[int] = []

    class _Model:
        def predict(self, pairs):
            called.append(len(pairs))
            return [0.5, 0.4]

    monkeypatch.setattr(reranker, "get_reranker", lambda: _Model())

    reranker.rerank("budget", [_hit(""), _hit("")])

    assert called == [2], (
        "an undetected language was counted as non-English, so every pre-detection "
        "library silently lost reranking"
    )


def test_the_share_is_measured_over_voting_hits_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two Spanish and one English among six, four unknown — Spanish is the majority.

    Counting unknowns as English would make this pool look 4-in-6 English and rerank
    it; the decision has to turn on language, not on library age.
    """
    from app.services.chat import reranker

    monkeypatch.setattr(
        reranker, "get_reranker", lambda: pytest.fail("the English reranker was invoked")
    )
    hits = [_hit("es"), _hit("es"), _hit("en"), _hit(""), _hit(""), _hit("")]

    assert reranker.rerank("presupuesto", hits) is hits


def test_the_language_reaches_the_hit_from_the_index() -> None:
    """The whole skip is inert if ``ChunkHit`` never carries the field.

    ``language`` is a mapped keyword on every chunk document, but it was not among the
    ``_source`` fields requested nor a field on the dataclass — so every hit would
    have reported ``""`` and the skip would never have fired on anything.
    """
    from app.services.search.chunk_retrieval import ChunkHit

    assert "language" in ChunkHit.__dataclass_fields__, (
        "ChunkHit has no language field, so every hit votes 'unknown' and the "
        "non-English skip can never fire"
    )
