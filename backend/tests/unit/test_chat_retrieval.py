"""Retrieval pipeline behaviour for RAG chat (issue #52).

Covers the parts that decide answer quality: diversity across files, rerank
ordering, the adaptive RRF window, and the cache's key discipline.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit
from app.services.search.chunk_retrieval import diversity_sample
from app.services.search.chunk_retrieval import dynamic_rrf_window
from app.services.search.chunk_retrieval import retrieve_chunks
from app.services.search.hybrid_search_service import HybridSearchService
from tests.helpers import does_not_raise


def _hit(file_uuid: str, index: int, score: float = 1.0) -> ChunkHit:
    return ChunkHit(
        file_uuid=file_uuid,
        file_id=abs(hash(file_uuid)) % 1000,
        chunk_index=index,
        content=f"{file_uuid} chunk {index}",
        title=f"Recording {file_uuid}",
        speaker="Dana",
        start_time=float(index * 30),
        end_time=float(index * 30 + 30),
        score=score,
    )


# ---------------------------------------------------------------------------
# Diversity sampling — the multi-transcript guarantee
# ---------------------------------------------------------------------------


def test_diversity_prevents_one_file_monopolizing_context():
    """The whole point of multi-file chat: every selected file gets a voice."""
    hits = [_hit("long-recording", i) for i in range(20)]
    hits += [_hit("short-a", 0), _hit("short-b", 0)]

    selected = diversity_sample(hits, max_per_file=4, cap=12)

    files = {hit.file_uuid for hit in selected}
    assert files == {"long-recording", "short-a", "short-b"}
    assert sum(1 for h in selected if h.file_uuid == "long-recording") <= 4


def test_diversity_visits_best_file_first():
    hits = [_hit("best", 0), _hit("best", 1), _hit("second", 0)]
    selected = diversity_sample(hits, max_per_file=4, cap=10)
    assert selected[0].file_uuid == "best"
    assert selected[1].file_uuid == "second"  # round-robin, not depth-first


def test_diversity_respects_the_total_cap():
    hits = [_hit(f"file-{f}", i) for f in range(10) for i in range(10)]
    selected = diversity_sample(hits, max_per_file=4, cap=7)
    assert len(selected) == 7


def test_diversity_returns_everything_when_under_cap():
    hits = [_hit("a", 0), _hit("b", 0)]
    assert len(diversity_sample(hits, max_per_file=4, cap=12)) == 2


def test_diversity_handles_empty_input():
    assert diversity_sample([], max_per_file=4, cap=12) == []


def test_diversity_never_duplicates_a_chunk():
    hits = [_hit("a", i) for i in range(5)] + [_hit("b", i) for i in range(5)]
    selected = diversity_sample(hits, max_per_file=3, cap=10)
    keys = [(h.file_uuid, h.chunk_index) for h in selected]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Adaptive RRF window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [(1, 100), (12, 100), (48, 192), (200, 500), (5000, 500)],
)
def test_dynamic_rrf_window_scales_and_clamps(size, expected):
    assert dynamic_rrf_window(size) == expected


# ---------------------------------------------------------------------------
# retrieve_chunks — authorization and degradation
# ---------------------------------------------------------------------------


def test_empty_resolved_scope_matches_nothing_not_everything():
    """A scope that resolved to zero files must NOT fall back to all transcripts."""
    with patch("app.services.search.chunk_retrieval.get_opensearch_client") as client:
        assert retrieve_chunks("q", user_id=1, file_uuids=[]) == []
        client.assert_not_called()


def test_blank_query_short_circuits():
    with patch("app.services.search.chunk_retrieval.get_opensearch_client") as client:
        assert retrieve_chunks("   ", user_id=1) == []
        client.assert_not_called()


def test_retrieval_failure_degrades_to_no_context():
    """OpenSearch down must yield a context-free answer, never a 500."""
    client = MagicMock()
    client.search.side_effect = RuntimeError("opensearch down")
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        assert retrieve_chunks("q", user_id=1) == []


def test_retrieval_always_filters_by_caller():
    """The accessible_user_ids term is the isolation gate — it must always be sent."""
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=42, file_uuids=["u1"], search_mode="keyword")

    body = client.search.call_args.kwargs["body"]
    filters = body["query"]["bool"]["filter"]
    assert {"terms": {"accessible_user_ids": [42]}} in filters
    assert {"terms": {"file_uuid": ["u1"]}} in filters


def test_retrieval_parses_hits_into_chunks():
    client = MagicMock()
    client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 3.5,
                    "_source": {
                        "file_uuid": "abc",
                        "file_id": 7,
                        "chunk_index": 2,
                        "content": "the budget was approved",
                        "title": "Board meeting",
                        "speaker": "Chair",
                        "start_time": 120.5,
                        "end_time": 150.0,
                    },
                },
                {"_score": 1.0, "_source": {"file_uuid": "abc"}},  # no content → dropped
            ]
        }
    }
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        chunks = retrieve_chunks("budget", user_id=1, search_mode="keyword")

    assert len(chunks) == 1
    assert chunks[0].content == "the budget was approved"
    assert chunks[0].speaker == "Chair"
    assert chunks[0].start_time == 120.5
    assert chunks[0].score == 3.5


# ---------------------------------------------------------------------------
# Cache keying
# ---------------------------------------------------------------------------


def test_cache_key_separates_users_orgs_and_scopes():
    from app.services.chat import retrieval_cache

    scope_digest = retrieval_cache.scope_hash(["a", "b"])

    def key(user_id: int, organization_id: int | None) -> str:
        return retrieval_cache.cache_key(
            user_id=user_id,
            organization_id=organization_id,
            query="what was decided",
            scope_digest=scope_digest,
            settings_rev="rev1",
            search_mode="hybrid",
        )

    assert len({key(1, None), key(2, None), key(1, 9)}) == 3


def test_cache_key_is_stable_across_query_casing_and_spacing():
    from app.services.chat import retrieval_cache

    def key(query: str) -> str:
        return retrieval_cache.cache_key(
            user_id=1,
            organization_id=None,
            query=query,
            scope_digest="s",
            settings_rev="r",
            search_mode="hybrid",
        )

    assert key("What Was  Decided?") == key("what was decided?")


def test_settings_revision_changes_when_retrieval_knobs_change():
    """An admin retune must invalidate cached results, not reuse the old shape."""
    base = ChatSettings()
    retuned = ChatSettings(final_chunks=base.final_chunks + 5)
    assert base.revision != retuned.revision


def test_scope_hash_is_order_independent():
    from app.services.chat import retrieval_cache

    assert retrieval_cache.scope_hash(["b", "a"]) == retrieval_cache.scope_hash(["a", "b"])
    # "all accessible" is stable and distinct from any explicit selection.
    assert retrieval_cache.scope_hash(None) == retrieval_cache.scope_hash(None)


def test_cache_key_changes_with_the_text_field_preset():
    """#506: retrieve_chunks/retrieve_digests now accept a BM25 `text_fields`
    override (the no-stemmed-leg arm). Without the resolved preset in the cache
    key, a request run under one preset would silently serve its cached hits to
    a request asking for a different preset — voiding the whole comparison.
    """
    from app.services.chat import retrieval_cache

    def key(preset: str) -> str:
        return retrieval_cache.cache_key(
            user_id=1,
            organization_id=None,
            query="what was decided",
            scope_digest="s",
            settings_rev="r",
            search_mode="hybrid",
            corpus_rev="0",
            text_fields_preset=preset,
        )

    assert key("default") != key("no-stem")
    # Omitting the parameter must be identical to explicitly asking for "default" —
    # every caller written before presets existed keeps hitting the same bucket.
    assert key("default") == retrieval_cache.cache_key(
        user_id=1,
        organization_id=None,
        query="what was decided",
        scope_digest="s",
        settings_rev="r",
        search_mode="hybrid",
        corpus_rev="0",
    )
    assert retrieval_cache.scope_hash(None) != retrieval_cache.scope_hash(["a"])


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------


def test_rerank_reorders_by_cross_encoder_score():
    from app.services.chat import reranker

    hits = [_hit("a", 0), _hit("a", 1), _hit("a", 2)]
    model = MagicMock()
    model.predict.return_value = [0.1, 0.9, 0.5]

    with patch.object(reranker, "get_reranker", return_value=model):
        reordered = reranker.rerank("q", hits, max_pairs=10)

    assert [h.chunk_index for h in reordered] == [1, 2, 0]
    assert reordered[0].score == pytest.approx(0.9)


def test_rerank_is_a_noop_without_the_model():
    """A missing model cache disables reranking, it does not break chat."""
    from app.services.chat import reranker

    hits = [_hit("a", 0), _hit("a", 1)]
    with patch.object(reranker, "get_reranker", return_value=None):
        assert reranker.rerank("q", hits) == hits


def test_rerank_survives_model_failure():
    from app.services.chat import reranker

    hits = [_hit("a", 0), _hit("a", 1)]
    model = MagicMock()
    model.predict.side_effect = RuntimeError("boom")
    with patch.object(reranker, "get_reranker", return_value=model):
        assert reranker.rerank("q", hits) == hits


def test_rerank_leaves_the_tail_beyond_max_pairs_in_place():
    from app.services.chat import reranker

    hits = [_hit("a", i) for i in range(5)]
    model = MagicMock()
    model.predict.return_value = [0.1, 0.2]  # only the first 2 scored

    with patch.object(reranker, "get_reranker", return_value=model):
        reordered = reranker.rerank("q", hits, max_pairs=2)

    assert len(reordered) == 5
    assert [h.chunk_index for h in reordered[2:]] == [2, 3, 4]


# ---------------------------------------------------------------------------
# Semantic cache (tier 2, opt-in)
# ---------------------------------------------------------------------------


def test_cosine_similarity_bounds():
    from app.services.chat.retrieval_cache import _cosine

    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # Degenerate inputs must score 0, never raise or divide by zero.
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert _cosine([1.0], [1.0, 0.0]) == 0.0


def test_semantic_cache_reuses_a_rephrased_question():
    """The point of tier 2: a rewording hits the same cached retrieval."""
    from app.services.chat import retrieval_cache

    hits = [_hit("a", 0)]
    entry = {
        "key": "chat:retr:1:0:abc",
        "vector": [1.0, 0.0, 0.0],
        "scope": "scope1",
        "rev": "rev1",
        "corpus": "5",
    }
    redis = MagicMock()
    redis.get.return_value = json.dumps([entry])

    with (
        patch.object(retrieval_cache, "_embed_query", return_value=[0.99, 0.01, 0.0]),
        patch.object(retrieval_cache, "corpus_version", return_value="5"),
        patch("app.core.redis.get_redis", return_value=redis),
        patch.object(retrieval_cache, "get_cached", return_value=hits),
    ):
        result = retrieval_cache.find_semantic_match(
            user_id=1,
            organization_id=None,
            query="what was the pricing decision",
            scope_digest="scope1",
            settings_rev="rev1",
            threshold=0.97,
        )

    assert result is not None
    assert result[0] == hits


def test_semantic_cache_misses_below_threshold():
    """A merely related question must NOT reuse another question's passages."""
    from app.services.chat import retrieval_cache

    entry = {"key": "k", "vector": [1.0, 0.0], "scope": "s", "rev": "r", "corpus": "1"}
    redis = MagicMock()
    redis.get.return_value = json.dumps([entry])

    with (
        patch.object(retrieval_cache, "_embed_query", return_value=[0.0, 1.0]),
        patch.object(retrieval_cache, "corpus_version", return_value="1"),
        patch("app.core.redis.get_redis", return_value=redis),
    ):
        assert (
            retrieval_cache.find_semantic_match(
                user_id=1,
                organization_id=None,
                query="something unrelated",
                scope_digest="s",
                settings_rev="r",
                threshold=0.97,
            )
            is None
        )


def test_semantic_cache_never_crosses_scope_or_settings():
    """Same wording, different files (or retuned settings) must not reuse."""
    from app.services.chat import retrieval_cache

    entries = [
        {"key": "k1", "vector": [1.0, 0.0], "scope": "OTHER_SCOPE", "rev": "r", "corpus": "1"},
        {"key": "k2", "vector": [1.0, 0.0], "scope": "s", "rev": "OTHER_REV", "corpus": "1"},
    ]
    redis = MagicMock()
    redis.get.return_value = json.dumps(entries)

    with (
        patch.object(retrieval_cache, "_embed_query", return_value=[1.0, 0.0]),
        patch.object(retrieval_cache, "corpus_version", return_value="1"),
        patch("app.core.redis.get_redis", return_value=redis),
    ):
        assert (
            retrieval_cache.find_semantic_match(
                user_id=1,
                organization_id=None,
                query="identical question",
                scope_digest="s",
                settings_rev="r",
                threshold=0.9,
            )
            is None
        )


def test_semantic_cache_degrades_when_embedding_unavailable():
    """No embedding model deployed → plain miss, never an error."""
    from app.services.chat import retrieval_cache

    with patch.object(retrieval_cache, "_embed_query", return_value=None):
        assert (
            retrieval_cache.find_semantic_match(
                user_id=1,
                organization_id=None,
                query="q",
                scope_digest="s",
                settings_rev="r",
                threshold=0.97,
            )
            is None
        )


class _SpecCompliantEvalFake:
    """A Redis double that actually implements `_REMEMBER_SCRIPT`'s CONTRACT.

    #403 W2.6: `remember_semantic` moved from a plain GET-then-SETEX (racy
    under a parallel fan-out) to a single atomic `EVAL`. A bare `MagicMock`
    can record that `.eval()` was called but proves nothing about what the
    real Lua script running server-side would do; this fake instead performs
    the exact append-trim-write contract the script promises (append one
    JSON-decoded entry, keep only the last N, write back), keyed on the
    SAME positional `ARGV` order `remember_semantic` sends. It is not a Lua
    interpreter — it is a spec double for the one script this module ships,
    which is what makes it able to catch a broken ARGV order without needing
    a live Redis.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def eval(self, _script: str, numkeys: int, *args: object) -> int:
        assert numkeys == 1
        key = str(args[0])
        ttl, entry_json, max_history = args[1], args[2], args[3]
        entries = json.loads(self.store[key]) if key in self.store else []
        entries.append(json.loads(str(entry_json)))
        # str() first: the *args signature is `object`, and int(object) has no
        # overload. The real client receives these as strings over the wire.
        entries = entries[-int(str(max_history)) :]
        self.store[key] = json.dumps(entries)
        assert int(str(ttl)) > 0
        return 1


def test_semantic_cache_history_is_bounded():
    """The per-user entry list must not grow without limit."""
    from app.services.chat import retrieval_cache

    existing = [{"key": f"k{i}", "vector": [1.0], "scope": "s", "rev": "r"} for i in range(60)]
    fake = _SpecCompliantEvalFake()
    fake.store[retrieval_cache._SEMANTIC_KEY.format(user_id=1, org=0, leg_kind="main")] = (
        json.dumps(existing)
    )

    with patch("app.core.redis.get_redis", return_value=fake):
        retrieval_cache.remember_semantic(
            user_id=1,
            organization_id=None,
            query="q",
            cache_key_value="new-key",
            scope_digest="s",
            settings_rev="r",
            ttl_seconds=300,
            embedding=[1.0],
        )

    stored = json.loads(
        fake.store[retrieval_cache._SEMANTIC_KEY.format(user_id=1, org=0, leg_kind="main")]
    )
    assert len(stored) == 50
    assert stored[-1]["key"] == "new-key"


def test_semantic_cache_write_is_atomic_via_a_single_eval_call():
    """#403 W2.6: no more read-then-write — one round trip, one script.

    The old shape (`client.get` then a later `client.setex`) races under a
    parallel fan-out: two legs finishing in the same window both read the
    same starting list and whichever `setex` lands second silently discards
    the other leg's entry. `EVAL` runs server-side as one atomic step, so
    there is exactly one write call and it is `eval`, never `setex`.
    """
    from app.services.chat import retrieval_cache

    redis = MagicMock()
    redis.get.return_value = None
    with patch("app.core.redis.get_redis", return_value=redis):
        retrieval_cache.remember_semantic(
            user_id=1,
            organization_id=None,
            query="q",
            cache_key_value="new-key",
            scope_digest="s",
            settings_rev="r",
            ttl_seconds=300,
            embedding=[1.0],
        )

    redis.eval.assert_called_once()
    redis.setex.assert_not_called()
    # Real data flow, not just call bookkeeping: the entry the script will
    # append is the one this call actually produced.
    sent_entry = json.loads(redis.eval.call_args[0][4])
    assert sent_entry["key"] == "new-key"
    assert sent_entry["vector"] == [1.0]


def test_semantic_cache_leg_kind_namespaces_the_redis_key():
    """#403 W2.6: without this, a sub-question leg reuses its PARENT's cache.

    A sub-question is frequently near-paraphrastic to the question it split
    from, and at the default 0.97 threshold that would resolve to the
    parent's own cached hits — the fan-out would then return nothing new
    while reporting success. Namespacing by `leg_kind` is what keeps a
    `"subquestion"` leg's remembered entries out of a `"main"` leg's lookup
    and vice versa.
    """
    from app.services.chat import retrieval_cache

    main_redis = MagicMock()
    with patch("app.core.redis.get_redis", return_value=main_redis):
        retrieval_cache.remember_semantic(
            user_id=1,
            organization_id=None,
            query="q",
            cache_key_value="k",
            scope_digest="s",
            settings_rev="r",
            ttl_seconds=300,
            embedding=[1.0],
            leg_kind="main",
        )
    main_key = main_redis.eval.call_args[0][2]

    subq_redis = MagicMock()
    with patch("app.core.redis.get_redis", return_value=subq_redis):
        retrieval_cache.remember_semantic(
            user_id=1,
            organization_id=None,
            query="q",
            cache_key_value="k",
            scope_digest="s",
            settings_rev="r",
            ttl_seconds=300,
            embedding=[1.0],
            leg_kind="subquestion",
        )
    subq_key = subq_redis.eval.call_args[0][2]

    assert main_key != subq_key


def test_semantic_cache_write_is_skipped_when_caching_disabled():
    from app.services.chat import retrieval_cache

    redis = MagicMock()
    with patch("app.core.redis.get_redis", return_value=redis):
        retrieval_cache.remember_semantic(
            user_id=1,
            organization_id=None,
            query="q",
            cache_key_value="k",
            scope_digest="s",
            settings_rev="r",
            ttl_seconds=0,
            embedding=[1.0],
        )
    redis.setex.assert_not_called()


def test_semantic_cache_is_skipped_when_disabled_in_settings():
    """The admin toggle must actually gate the tier-2 path."""
    from app.services.chat import retrieval

    with (
        patch.object(retrieval, "retrieve_chunks", return_value=[]),
        patch("app.services.chat.retrieval_cache.get_cached", return_value=None),
        patch("app.services.chat.retrieval_cache.set_cached"),
        patch("app.services.chat.retrieval_cache.find_semantic_match") as semantic,
    ):
        retrieval.retrieve_context(
            query="q",
            user_id=1,
            organization_id=None,
            file_uuids=None,
            settings=ChatSettings(semantic_cache_enabled=False),
        )
    semantic.assert_not_called()


# ---------------------------------------------------------------------------
# Corpus versioning — the cache must not outlive the content it describes
# ---------------------------------------------------------------------------


def _key_with_corpus(corpus: str) -> str:
    from app.services.chat import retrieval_cache

    return retrieval_cache.cache_key(
        user_id=1,
        organization_id=None,
        query="what was decided",
        scope_digest="s",
        settings_rev="r",
        search_mode="hybrid",
        corpus_rev=corpus,
    )


def test_cache_key_changes_when_corpus_changes():
    """Re-transcribing, editing or deleting content must strand old entries."""
    assert _key_with_corpus("1") != _key_with_corpus("2")


def test_cache_key_is_stable_within_one_corpus_version():
    assert _key_with_corpus("7") == _key_with_corpus("7")


def test_indexing_bumps_the_corpus_version():
    """New transcript content invalidates every cached retrieval."""
    from app.services.search import indexing_service

    with patch("app.services.chat.retrieval_cache.bump_corpus_version") as bump:
        indexing_service._invalidate_chat_retrieval_cache()
    bump.assert_called_once()


def test_corpus_bump_failure_never_breaks_indexing():
    """A Redis outage must not fail transcription indexing."""
    from app.services.search import indexing_service

    with patch(
        "app.services.chat.retrieval_cache.bump_corpus_version",
        side_effect=RuntimeError("redis down"),
    ) as bump:
        with does_not_raise("a Redis outage must not fail transcription indexing"):
            indexing_service._invalidate_chat_retrieval_cache()

    bump.assert_called_once()


def test_corpus_version_degrades_to_zero_without_redis():
    from app.services.chat import retrieval_cache

    with patch("app.core.redis.get_redis", side_effect=RuntimeError("no redis")):
        assert retrieval_cache.corpus_version() == "0"


def test_semantic_cache_ignores_entries_from_an_older_corpus():
    """A rephrased question must not reuse passages from changed content."""
    from app.services.chat import retrieval_cache

    entry = {
        "key": "k",
        "vector": [1.0, 0.0],
        "scope": "s",
        "rev": "r",
        "corpus": "1",  # stale: the corpus has moved on
    }
    redis = MagicMock()
    redis.get.return_value = json.dumps([entry])

    with (
        patch.object(retrieval_cache, "_embed_query", return_value=[1.0, 0.0]),
        patch.object(retrieval_cache, "corpus_version", return_value="2"),
        patch("app.core.redis.get_redis", return_value=redis),
    ):
        assert (
            retrieval_cache.find_semantic_match(
                user_id=1,
                organization_id=None,
                query="identical question",
                scope_digest="s",
                settings_rev="r",
                threshold=0.9,
            )
            is None
        )


# ---------------------------------------------------------------------------
# Speaker scoping — the transcript-native filter
# ---------------------------------------------------------------------------


def test_speaker_filter_reaches_opensearch():
    """Chunks are speaker turns, so this filter is exact 'only their words'."""
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=1, speakers=["Dana", "Ravi"], search_mode="keyword")

    filters = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert {"terms": {"speaker": ["Dana", "Ravi"]}} in filters


def test_no_speaker_filter_when_unset():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=1, speakers=[], search_mode="keyword")

    filters = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert not any("speaker" in f.get("terms", {}) for f in filters)


def test_speakers_are_part_of_the_cache_identity():
    """Same question, different speaker MUST NOT reuse the other's passages."""
    from app.services.chat import retrieval_cache

    dana = retrieval_cache.scope_hash(None, ["Dana"])
    ravi = retrieval_cache.scope_hash(None, ["Ravi"])
    nobody = retrieval_cache.scope_hash(None, [])
    assert len({dana, ravi, nobody}) == 3


def test_speaker_scope_hash_is_order_independent():
    from app.services.chat import retrieval_cache

    assert retrieval_cache.scope_hash(None, ["b", "a"]) == retrieval_cache.scope_hash(
        None, ["a", "b"]
    )


def test_speaker_only_scope_still_searches_all_recordings():
    """'Everything Dana said, anywhere' is a valid scope."""
    from app.schemas.chat import ChatScope

    scope = ChatScope(speakers=["Dana"])
    # Speakers deliberately don't make the RECORDING scope non-empty, so file
    # resolution still yields "all accessible".
    assert scope.is_empty is True
    assert scope.speakers == ["Dana"]


def test_speaker_scope_is_length_validated():
    from pydantic import ValidationError

    from app.schemas.chat import ChatScope

    with pytest.raises(ValidationError):
        ChatScope(speakers=[""])
    with pytest.raises(ValidationError):
        ChatScope(speakers=["x" * 201])


# ---------------------------------------------------------------------------
# Search modes must be genuinely different queries
# ---------------------------------------------------------------------------


def _body_for(mode: str) -> dict:
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with (
        patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client),
        patch.object(
            HybridSearchService, "_generate_query_embedding", return_value=(None, True, True)
        ),
        patch.object(HybridSearchService, "_get_neural_model_id", return_value="model-1"),
    ):
        retrieve_chunks("what was decided", user_id=1, search_mode=mode)
    kwargs: dict = client.search.call_args.kwargs
    return kwargs


def test_hybrid_mode_sends_both_legs():
    body = _body_for("hybrid")["body"]
    assert "hybrid" in body["query"]
    assert len(body["query"]["hybrid"]["queries"]) == 2


def test_semantic_mode_sends_only_the_vector_leg():
    """Regression: semantic and hybrid built an identical query, so the
    user-facing three-way selector had only two distinct behaviours."""
    kwargs = _body_for("semantic")
    body = kwargs["body"]

    assert "hybrid" not in body["query"]
    must = body["query"]["bool"]["must"]
    assert len(must) == 1
    assert "neural" in must[0]
    # No BM25 leg means nothing to fuse — the RRF pipeline would be dead weight.
    assert not kwargs.get("params")


def test_keyword_mode_sends_no_vector_leg():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("exact phrase", user_id=1, search_mode="keyword")

    body = client.search.call_args.kwargs["body"]
    assert "hybrid" not in body["query"]
    assert "neural" not in str(body["query"])


def test_every_mode_still_carries_the_authorization_filter():
    """Whatever the mode, isolation is non-negotiable."""
    for mode in ("hybrid", "semantic", "keyword"):
        client = MagicMock()
        client.search.return_value = {"hits": {"hits": []}}
        with (
            patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client),
            patch.object(
                HybridSearchService, "_generate_query_embedding", return_value=(None, True, True)
            ),
            patch.object(HybridSearchService, "_get_neural_model_id", return_value="model-1"),
        ):
            retrieve_chunks("q", user_id=42, search_mode=mode)

        assert '"accessible_user_ids": [42]' in str(client.search.call_args.kwargs["body"]).replace(
            "'", '"'
        ), f"mode={mode} lost its authorization filter"
