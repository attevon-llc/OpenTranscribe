"""The search response cache must key on the redaction policy, not just `user_id`.

Real, pre-existing leak (issue #86-adjacent): `_make_cache_key` used to be computed
before the redaction config was resolved, and `_redact_snippets` resolved it again
only AFTER a cache miss. So for up to `SEARCH_CACHE_TTL_SECONDS`, a policy change (a
user flipping masking on/off, or an admin floor changing) had no effect on a repeated
query — the response cached under the OLD policy kept being served. Two users on
different policies who happened to run the identical query within that window could
likewise serve each other's cached page.

`_resolve_redaction_config_for_cache` now runs BEFORE the cache lookup and
`_redaction_policy_fingerprint(cfg)` is folded into `_make_cache_key` as
`redaction_policy`. This module pins both halves: the fingerprint function in
isolation, and the real `HybridSearchService.search()` driven end to end against its
real module-level cache — only the OpenSearch client and the DB-backed config
resolution are stubbed, so the caching machinery that could actually leak is
exercised for real, matching `test_search_fusion_threading.py`'s
`TestResponseCacheKeysOnTheStrategy` pattern for the sibling fusion-pipeline key.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.redaction.config import EffectiveRedactionConfig
from app.services.search import hybrid_search_service as hss

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("search_cache_state")]

_MASKED = EffectiveRedactionConfig(enabled=True, enabled_categories={"pii"})
_UNMASKED = EffectiveRedactionConfig(enabled=False, enabled_categories=set())


@pytest.fixture(autouse=True)
def _clean_cache_state():
    """No cached page or verified-pipeline id survives into or out of a test here."""
    hss.reset_infrastructure_state()
    hss._search_cache.clear()
    yield
    hss.reset_infrastructure_state()
    hss._search_cache.clear()


# --------------------------------------------------------------------------- #
# The fingerprint function in isolation
# --------------------------------------------------------------------------- #


class TestRedactionPolicyFingerprint:
    def test_masking_on_and_off_produce_different_fingerprints(self):
        assert hss._redaction_policy_fingerprint(_MASKED) != hss._redaction_policy_fingerprint(
            _UNMASKED
        )

    def test_category_set_order_does_not_matter(self):
        a = EffectiveRedactionConfig(enabled=True, enabled_categories={"pii", "profanity"})
        b = EffectiveRedactionConfig(enabled=True, enabled_categories={"profanity", "pii"})
        assert hss._redaction_policy_fingerprint(a) == hss._redaction_policy_fingerprint(b)

    def test_a_different_category_set_moves_the_fingerprint(self):
        pii_only = EffectiveRedactionConfig(enabled=True, enabled_categories={"pii"})
        profanity_only = EffectiveRedactionConfig(enabled=True, enabled_categories={"profanity"})
        assert hss._redaction_policy_fingerprint(pii_only) != hss._redaction_policy_fingerprint(
            profanity_only
        )

    def test_custom_words_move_the_fingerprint(self):
        base = EffectiveRedactionConfig(
            enabled=True, enabled_categories={"custom"}, custom_words=["alpha"]
        )
        changed = dataclasses.replace(base, custom_words=["beta"])
        assert hss._redaction_policy_fingerprint(base) != hss._redaction_policy_fingerprint(changed)

    def test_a_toxicity_only_category_that_masks_nothing_here_is_unmasked(self):
        """`toxicity` has no maskable spans on this surface — same as `enabled=False`."""
        toxicity_only = EffectiveRedactionConfig(enabled=True, enabled_categories={"toxicity"})
        assert hss._redaction_policy_fingerprint(
            toxicity_only
        ) == hss._redaction_policy_fingerprint(_UNMASKED)

    def test_style_and_toxicity_threshold_do_not_move_it(self):
        """Neither field can change `mask_snippets`'s output on this surface:

        previews always render `style="label"` regardless of the user's own
        preference, and `toxicity` produces no spans here at all.
        """
        a = EffectiveRedactionConfig(
            enabled=True, enabled_categories={"pii"}, style="blur", toxicity_threshold=0.1
        )
        b = dataclasses.replace(a, style="asterisks", toxicity_threshold=0.9)
        assert hss._redaction_policy_fingerprint(a) == hss._redaction_policy_fingerprint(b)

    def test_unresolvable_gets_its_own_fixed_bucket(self):
        assert hss._redaction_policy_fingerprint(None) == "unresolvable"
        assert hss._redaction_policy_fingerprint(None) != hss._redaction_policy_fingerprint(
            _UNMASKED
        )


class TestCacheKeyFoldsInTheFingerprint:
    def test_masking_on_vs_off_produce_different_cache_keys(self):
        key_masked = hss._make_cache_key(
            query="q", user_id=1, redaction_policy=hss._redaction_policy_fingerprint(_MASKED)
        )
        key_unmasked = hss._make_cache_key(
            query="q", user_id=1, redaction_policy=hss._redaction_policy_fingerprint(_UNMASKED)
        )
        assert key_masked != key_unmasked


# --------------------------------------------------------------------------- #
# The real `search()`, its real cache, and a real per-policy miss
# --------------------------------------------------------------------------- #


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
    # The masking call itself is not what this suite is proving — the CACHE KEY is.
    # `_empty_response` carries no results to mask anyway; stubbed to keep the seam
    # explicit rather than relying on that shape never changing.
    monkeypatch.setattr(
        hss.HybridSearchService, "_redact_snippets", lambda self, result, user_id, cfg: None
    )
    return calls


def _resolve_policy_sequence(monkeypatch, configs: list):
    """`_resolve_redaction_config_for_cache` returns `configs` in call order."""
    it = iter(configs)
    monkeypatch.setattr(hss, "_resolve_redaction_config_for_cache", lambda user_id: next(it))


class TestLiveSearchDoesNotCollideAcrossPolicies:
    def test_two_policies_for_the_identical_query_do_not_share_a_cached_page(
        self, monkeypatch, _wired_service
    ):
        """The middle call is the control: it proves the cache really caches, so the

        third call missing the cache means the KEY changed — not that caching broke.
        """
        _resolve_policy_sequence(monkeypatch, [_MASKED, _MASKED, _UNMASKED])
        service = hss.HybridSearchService()

        service.search("budget", user_id=7)
        assert len(_wired_service) == 1

        service.search("budget", user_id=7)
        assert len(_wired_service) == 1, "the cache did not serve the repeat; the control is broken"

        service.search("budget", user_id=7)
        assert len(_wired_service) == 2, "a different policy replayed the other policy's page"

    def test_an_unresolvable_policy_does_not_collide_with_a_resolved_one(
        self, monkeypatch, _wired_service
    ):
        _resolve_policy_sequence(monkeypatch, [_MASKED, None])
        service = hss.HybridSearchService()

        service.search("budget", user_id=7)
        service.search("budget", user_id=7)

        assert len(_wired_service) == 2
