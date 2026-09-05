"""The search response cache must key on the corpus version too (issue #666).

`clear_search_cache()` used to be a bare `_search_cache.clear()` called from
whichever CPU/embedding worker ran a reindex or model switch — never the API
process that actually serves cached search responses, so it was a no-op in the
process that mattered. `_search_corpus_version()` reuses
`chat.retrieval_cache.corpus_version()` — the SAME Redis counter that already
gets bumped, cross-process, by every real content-changing chunk-plane write
(`indexing_service._invalidate_chat_retrieval_cache`) — as a cache-key field, so
a version bump makes every previously cached key permanently unreachable
without anyone needing to remember to clear a cache living in a different
process.

Follows the same real-`search()`-driven pattern as
`test_search_cache_redaction_key.py`'s `TestLiveSearchDoesNotCollideAcrossPolicies`.
"""

from __future__ import annotations

import pytest

from app.services.search import hybrid_search_service as hss

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("search_cache_state")]


@pytest.fixture(autouse=True)
def _clean_cache_state():
    hss.reset_infrastructure_state()
    hss._search_cache.clear()
    yield
    hss.reset_infrastructure_state()
    hss._search_cache.clear()


def test_clear_search_cache_no_longer_exists():
    """The old process-local, cross-process-blind mechanism must be GONE, not just unused.

    Repo rule: replacing an implementation means deleting the old one, never
    leaving two paths doing the same job.
    """
    assert not hasattr(hss, "clear_search_cache")


class TestCorpusVersionFoldsIntoTheCacheKey:
    def test_two_corpus_versions_produce_different_cache_keys(self):
        key_v0 = hss._make_cache_key(query="q", user_id=1, corpus_version="0")
        key_v1 = hss._make_cache_key(query="q", user_id=1, corpus_version="1")
        assert key_v0 != key_v1

    def test_a_redis_read_failure_degrades_to_a_fixed_version_rather_than_raising(
        self, monkeypatch
    ):
        def _raise():
            raise RuntimeError("redis is down")

        monkeypatch.setattr(
            "app.services.chat.retrieval_cache.corpus_version",
            lambda: (_ for _ in ()).throw(RuntimeError("redis is down")),
        )
        assert hss._search_corpus_version() == "0"

    def test_reads_the_same_counter_chat_retrieval_bumps(self, monkeypatch):
        """Not a second counter — literally `chat.retrieval_cache.corpus_version`."""
        from app.services.chat import retrieval_cache

        monkeypatch.setattr(retrieval_cache, "corpus_version", lambda: "42")
        assert hss._search_corpus_version() == "42"


@pytest.fixture
def _wired_service(monkeypatch):
    """`HybridSearchService.search()` with only the network/DB seams stubbed."""
    calls: list[str] = []

    def _capture(self, **kwargs):
        calls.append("call")
        return self._empty_response(kwargs["query"], kwargs["page"], kwargs["page_size"])

    monkeypatch.setattr(hss.HybridSearchService, "_search_with_collapse", _capture)
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: object())
    monkeypatch.setattr(
        hss.HybridSearchService,
        "_generate_query_embedding",
        lambda self, query, mode: (None, False, False),
    )
    monkeypatch.setattr(
        hss, "_ensure_infrastructure", lambda fusion=None: "transcript-hybrid-search"
    )
    monkeypatch.setattr(hss, "_resolve_redaction_config_for_cache", lambda user_id: None)
    monkeypatch.setattr(
        hss.HybridSearchService, "_redact_snippets", lambda self, result, user_id, cfg: None
    )
    return calls


def _corpus_version_sequence(monkeypatch, versions: list[str]):
    it = iter(versions)
    monkeypatch.setattr(hss, "_search_corpus_version", lambda: next(it))


class TestLiveSearchInvalidatesOnACorpusVersionBump:
    def test_a_corpus_version_bump_between_two_identical_queries_misses_the_cache(
        self, monkeypatch, _wired_service
    ):
        """The middle call is the control: it proves caching actually works, so the

        third call missing is because the VERSION moved, not because caching is
        broken. This is the scenario a real reindex produces: `index_transcript_chunks`
        bumps the corpus version for the edited file, and a repeat search for the
        old text (or the new text) must not be served whatever was cached before
        the bump.
        """
        _corpus_version_sequence(monkeypatch, ["1", "1", "2"])
        service = hss.HybridSearchService()

        service.search("budget", user_id=7)
        assert len(_wired_service) == 1

        service.search("budget", user_id=7)
        assert len(_wired_service) == 1, "the cache did not serve the repeat; the control is broken"

        service.search("budget", user_id=7)
        assert len(_wired_service) == 2, "a corpus-version bump replayed the pre-bump cached page"
