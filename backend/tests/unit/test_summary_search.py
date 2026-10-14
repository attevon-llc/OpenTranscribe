"""OpenSearch hybrid search over ``media_file.summary_data`` (issues #462, #963).

Covers the pure tree-walk helper and ``search_summaries``'s own orchestration —
masking per leaf BEFORE snippet extraction, the fail-closed contract on a
detector outage, quarantine-set passthrough, and dataclass shape. The actual
OpenSearch query (access control via ``accessible_user_ids``, the RRF fusion,
the real hybrid BM25+kNN retrieval) is a property of two systems talking to
each other and is covered against a real cluster in
``tests/integration/test_summary_plane_opensearch.py`` — no unit test with a
mocked query layer can prove that. Here, ``HybridSearchService.search_summary_plane``
is monkeypatched at the boundary so these tests are fast, DB-optional, and
exercise exactly the orchestration logic that lives in this module.
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
from app.services.search import summary_search
from app.services.search.summary_search import SummarySectionMatch
from app.services.search.summary_search import search_summaries
from app.services.search.summary_search import walk_summary_leaves

pytestmark = pytest.mark.unit

#: search_summaries's `db` param is unused (see its docstring) — a real
#: Postgres session buys nothing here, so a sentinel keeps this file DB-free.
_DB = cast(Session, object())


def _cfg(enabled: bool, categories: frozenset[str] = frozenset()) -> EffectiveRedactionConfig:
    """A real ``EffectiveRedactionConfig`` — ``resolve_summary_leaf_policy`` calls
    ``dataclasses.replace(cfg, ...)``, which requires a real dataclass instance,
    not a stand-in class."""
    return EffectiveRedactionConfig(enabled=enabled, enabled_categories=set(categories))


def _patch_plane(monkeypatch, hits: list[dict], total: int | None = None) -> list[tuple]:
    """Stand in for the real OpenSearch call; records every call's kwargs.

    Also stubs ``_quarantined_file_uuids`` to an empty set — it opens its OWN
    Postgres session (by design, see its docstring), which this file has no
    business paying for just to prove `search_summaries`'s orchestration.
    Tests exercising the quarantine-set passthrough itself override this.
    """
    calls: list[tuple] = []

    def _fake_search_summary_plane(self, query, user_id, **kwargs):
        calls.append((query, user_id, kwargs))
        return hits, total if total is not None else len(hits)

    monkeypatch.setattr(
        "app.services.search.hybrid_search_service.HybridSearchService.search_summary_plane",
        _fake_search_summary_plane,
    )
    monkeypatch.setattr("app.services.search.hybrid_search_service._quarantined_file_uuids", list)
    return calls


# --------------------------------------------------------------------------- #
# walk_summary_leaves — varied JSONB shapes, including extra="allow" keys      #
# --------------------------------------------------------------------------- #


class TestWalkSummaryLeaves:
    def test_a_bare_top_level_string(self):
        assert walk_summary_leaves("hello", "") == [("", "hello")]

    def test_flat_dict(self):
        leaves = walk_summary_leaves({"bluf": "one", "brief_summary": "two"}, "")
        assert dict(leaves) == {"bluf": "one", "brief_summary": "two"}

    def test_nested_dicts_and_lists(self):
        node = {"major_topics": [{"topic": "Budget", "key_points": ["alpha", "beta"]}]}
        leaves = walk_summary_leaves(node, "")
        assert leaves == [
            ("major_topics[0].topic", "Budget"),
            ("major_topics[0].key_points[0]", "alpha"),
            ("major_topics[0].key_points[1]", "beta"),
        ]

    def test_a_custom_prompt_key_extra_allow_is_walked(self):
        """SummaryData is extra="allow" — a custom prompt's own field names must
        be walked exactly like the known ones."""
        node = {"risk_register": {"items": [{"severity": "high", "detail": "leak"}]}}
        leaves = walk_summary_leaves(node, "")
        assert ("risk_register.items[0].severity", "high") in leaves
        assert ("risk_register.items[0].detail", "leak") in leaves

    def test_unicode_leaves(self):
        node = {"bluf": "Café résumé — 日本語のテスト"}
        assert walk_summary_leaves(node, "") == [("bluf", "Café résumé — 日本語のテスト")]

    def test_metadata_top_level_key_is_skipped(self):
        node = {"bluf": "kept", "metadata": {"provider": "openai", "created_at": "2026-01-01"}}
        leaves = walk_summary_leaves(node, "")
        assert leaves == [("bluf", "kept")]

    def test_non_string_leaves_are_never_emitted(self):
        node = {"counts": {"topics": 3, "ratio": 0.5, "flagged": True, "missing": None}}
        assert walk_summary_leaves(node, "") == []

    def test_blank_strings_are_never_emitted(self):
        assert walk_summary_leaves({"bluf": "   "}, "") == []

    def test_a_nested_metadata_key_is_not_special(self):
        """Only the TOP-LEVEL ``metadata`` key is machine-generated provenance;
        a section that happens to be named "metadata" one level down is
        ordinary model prose and must be walked."""
        node = {"major_topics": [{"metadata": "not actually special here"}]}
        leaves = walk_summary_leaves(node, "")
        assert leaves == [("major_topics[0].metadata", "not actually special here")]


# --------------------------------------------------------------------------- #
# search_summaries — orchestration: mapping, snippets, dataclass shape         #
# --------------------------------------------------------------------------- #


class TestSearchSummariesOrchestration:
    def test_a_hit_maps_into_a_summary_hit_with_its_match(self, monkeypatch):
        _patch_plane(
            monkeypatch,
            hits=[
                {
                    "file_uuid": "11111111-1111-1111-1111-111111111111",
                    "file_id": 42,
                    "title": "Q3 roadmap",
                    "matches": [
                        {
                            "key_path": "bluf",
                            "content": "The quarterly roadmap review.",
                            "score": 1.5,
                        }
                    ],
                }
            ],
        )
        result = search_summaries(_DB, "roadmap", 1, organization_id=None)
        assert result.total == 1
        assert len(result.results) == 1
        hit = result.results[0]
        assert hit.file_uuid == "11111111-1111-1111-1111-111111111111"
        assert hit.file_id == 42
        assert hit.title == "Q3 roadmap"
        assert hit.matches == [
            SummarySectionMatch(key_path="bluf", snippet="The quarterly roadmap review.", score=1.5)
        ]

    def test_a_missing_title_falls_back_to_the_empty_string(self, monkeypatch):
        _patch_plane(
            monkeypatch,
            hits=[{"file_uuid": "u", "file_id": 1, "title": "", "matches": []}],
        )
        result = search_summaries(_DB, "roadmap", 1, organization_id=None)
        assert result.results[0].title == ""

    def test_no_hits_returns_an_empty_result(self, monkeypatch):
        _patch_plane(monkeypatch, hits=[], total=0)
        result = search_summaries(_DB, "zzz_no_such_term", 1, organization_id=None)
        assert result.total == 0
        assert result.results == []

    def test_snippet_is_truncated_for_a_pathological_leaf(self, monkeypatch):
        long_text = "roadmap " + ("word " * 200)
        _patch_plane(
            monkeypatch,
            hits=[
                {
                    "file_uuid": "u",
                    "file_id": 1,
                    "title": "",
                    "matches": [{"key_path": "bluf", "content": long_text, "score": 1.0}],
                }
            ],
        )
        result = search_summaries(_DB, "roadmap", 1, organization_id=None)
        snippet = result.results[0].matches[0].snippet
        assert len(snippet) <= summary_search._MAX_SNIPPET_CHARS + 1  # +1 for the ellipsis char
        assert snippet.endswith("…")

    def test_filter_kwargs_and_quarantine_set_are_forwarded_to_the_plane_search(self, monkeypatch):
        """The whole point of #963's rewrite (closing #831): one filter builder,
        forwarded verbatim, not re-derived here."""
        calls = _patch_plane(monkeypatch, hits=[])
        monkeypatch.setattr(
            "app.services.search.hybrid_search_service._quarantined_file_uuids",
            lambda: ["deadbeef"],
        )

        search_summaries(
            _DB,
            "roadmap",
            7,
            organization_id=3,
            speakers=["Dana"],
            tags=["planning"],
            date_from="2024-01-01",
            date_to="2024-12-31",
            file_type=["audio"],
            collection_id=5,
            min_duration=1.0,
            max_duration=100.0,
            min_file_size=1,
            max_file_size=1000,
            language="en",
            title_filter="kickoff",
            file_uuid="some-uuid",
        )

        assert len(calls) == 1
        _query, user_id, kwargs = calls[0]
        assert user_id == 7
        assert kwargs["organization_id"] == 3
        assert kwargs["speakers"] == ["Dana"]
        assert kwargs["tags"] == ["planning"]
        assert kwargs["date_from"] == "2024-01-01"
        assert kwargs["date_to"] == "2024-12-31"
        assert kwargs["file_type"] == ["audio"]
        assert kwargs["collection_id"] == 5
        assert kwargs["title_filter"] == "kickoff"
        assert kwargs["file_uuid"] == "some-uuid"
        assert kwargs["quarantined_file_uuids"] == ["deadbeef"]

    def test_include_quarantined_skips_resolving_the_exclusion_set(self, monkeypatch):
        calls = _patch_plane(monkeypatch, hits=[])

        def _boom():
            raise AssertionError("quarantine resolution must not run for an admin review")

        monkeypatch.setattr(
            "app.services.search.hybrid_search_service._quarantined_file_uuids", _boom
        )

        search_summaries(_DB, "roadmap", 1, organization_id=None, include_quarantined=True)
        assert calls[0][2]["quarantined_file_uuids"] == []


# --------------------------------------------------------------------------- #
# Masking — per leaf, BEFORE snippet extraction, fail-closed, never batched    #
# --------------------------------------------------------------------------- #


class TestMasking:
    def test_no_cfg_returns_the_raw_snippet(self, monkeypatch):
        _patch_plane(
            monkeypatch,
            hits=[
                {
                    "file_uuid": "u",
                    "file_id": 1,
                    "title": "",
                    "matches": [
                        {"key_path": "bluf", "content": "Damn, that slipped.", "score": 1.0}
                    ],
                }
            ],
        )
        result = search_summaries(_DB, "slipped", 1, organization_id=None, redaction_cfg=None)
        assert "Damn" in result.results[0].matches[0].snippet

    def test_a_disabled_policy_leaves_the_snippet_untouched(self, monkeypatch):
        _patch_plane(
            monkeypatch,
            hits=[
                {
                    "file_uuid": "u",
                    "file_id": 1,
                    "title": "",
                    "matches": [
                        {"key_path": "bluf", "content": "Damn, that slipped.", "score": 1.0}
                    ],
                }
            ],
        )
        cfg = _cfg(False)
        result = search_summaries(_DB, "slipped", 1, organization_id=None, redaction_cfg=cfg)
        assert "Damn" in result.results[0].matches[0].snippet

    def test_an_enabled_policy_masks_the_returned_snippet(self, monkeypatch):
        _patch_plane(
            monkeypatch,
            hits=[
                {
                    "file_uuid": "u",
                    "file_id": 1,
                    "title": "",
                    "matches": [
                        {"key_path": "bluf", "content": "Damn, that slipped.", "score": 1.0}
                    ],
                }
            ],
        )
        cfg = _cfg(True, frozenset({"profanity"}))
        result = search_summaries(_DB, "slipped", 1, organization_id=None, redaction_cfg=cfg)
        assert "Damn" not in result.results[0].matches[0].snippet

    def test_masking_is_applied_per_returned_leaf_not_batched(self, monkeypatch):
        """The measured failure mode this brief calls out: a batched detector
        pass loses text across leaves (31 of 32 snippets, measured on the
        sibling search-snippet path). `search_summaries` must call the
        per-leaf `mask_summary_leaf` exactly once per RETURNED MATCH, never
        coalescing multiple leaves' text into one detection call.
        """
        _patch_plane(
            monkeypatch,
            hits=[
                {
                    "file_uuid": "u1",
                    "file_id": 1,
                    "title": "",
                    "matches": [{"key_path": "bluf", "content": "roadmap one", "score": 1.0}],
                },
                {
                    "file_uuid": "u2",
                    "file_id": 2,
                    "title": "",
                    "matches": [{"key_path": "bluf", "content": "roadmap two", "score": 1.0}],
                },
            ],
        )
        calls: list[str] = []
        real_mask_summary_leaf = summary_search.mask_summary_leaf

        def _spy(text, cfg):
            calls.append(text)
            return real_mask_summary_leaf(text, cfg)

        monkeypatch.setattr(summary_search, "mask_summary_leaf", _spy)
        cfg = _cfg(True, frozenset({"profanity"}))

        search_summaries(_DB, "roadmap", 1, organization_id=None, redaction_cfg=cfg)

        assert calls == ["roadmap one", "roadmap two"], (
            "mask_summary_leaf must be called once per matched leaf, individually — "
            "never with more than one leaf's text in a single call"
        )

    def test_a_detector_outage_on_a_returned_leaf_fails_closed(self, monkeypatch):
        _patch_plane(
            monkeypatch,
            hits=[
                {
                    "file_uuid": "u",
                    "file_id": 1,
                    "title": "",
                    "matches": [{"key_path": "bluf", "content": "roadmap review", "score": 1.0}],
                }
            ],
        )

        def _raise(*_args, **_kwargs):
            raise SummaryMaskingUnavailableError("pii detector unavailable")

        monkeypatch.setattr(summary_search, "mask_summary_leaf", _raise)
        cfg = _cfg(True, frozenset({"pii"}))

        with pytest.raises(SummaryMaskingUnavailableError):
            search_summaries(_DB, "roadmap", 1, organization_id=None, redaction_cfg=cfg)

    def test_a_file_with_no_matches_is_never_examined_by_the_masker(self, monkeypatch):
        """issue #822's narrowing, preserved: an empty ``matches`` list must
        never reach the masker at all."""
        _patch_plane(
            monkeypatch, hits=[{"file_uuid": "u", "file_id": 1, "title": "", "matches": []}]
        )

        def _boom(*_a, **_k):
            raise AssertionError("mask_summary_leaf must not run on a file with no matches")

        monkeypatch.setattr(summary_search, "mask_summary_leaf", _boom)
        cfg = _cfg(True, frozenset({"pii"}))

        result = search_summaries(_DB, "roadmap", 1, organization_id=None, redaction_cfg=cfg)
        assert result.results[0].matches == []
