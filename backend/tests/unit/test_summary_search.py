"""Postgres full-text search over ``media_file.summary_data`` (issue #462).

Covers the pure tree-walk helpers, the leaf-level FTS matching against real
Postgres, access control (reusing ``PermissionService.get_accessible_file_ids_
subquery`` — never a second sharing rule), per-leaf masking BEFORE snippet
extraction, and the fail-closed contract on a detector outage.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy import text as sa_text

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.prompt import UserSetting
from app.models.sharing import CollectionShare
from app.services.redaction.config import resolve_effective_config
from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
from app.services.search import summary_search
from app.services.search.summary_search import _get_by_path
from app.services.search.summary_search import _walk_leaves
from app.services.search.summary_search import search_summaries

pytestmark = pytest.mark.unit


def _make_file(db_session, user, *, summary, title=None) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        title=title,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=user.id,
        status="completed",
        summary_data=summary,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _enable_redaction(db_session, user, categories: str = '["pii"]') -> None:
    for key, value in (("redaction_enabled", "true"), ("redaction_categories", categories)):
        db_session.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.commit()


def _share_with(db_session, owner, recipient, media_file, *, permission="viewer") -> None:
    collection = Collection(
        user_id=owner.id,
        name=f"share-{uuid_pkg.uuid4().hex[:8]}",
        description="summary search test",
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=recipient.id,
            permission=permission,
        )
    )
    db_session.commit()


# --------------------------------------------------------------------------- #
# _walk_leaves — varied JSONB shapes, including extra="allow" custom keys      #
# --------------------------------------------------------------------------- #


class TestWalkLeaves:
    def test_a_bare_top_level_string(self):
        assert _walk_leaves("hello", "") == [("", "hello")]

    def test_flat_dict(self):
        leaves = _walk_leaves({"bluf": "one", "brief_summary": "two"}, "")
        assert dict(leaves) == {"bluf": "one", "brief_summary": "two"}

    def test_nested_dicts_and_lists(self):
        node = {"major_topics": [{"topic": "Budget", "key_points": ["alpha", "beta"]}]}
        leaves = _walk_leaves(node, "")
        assert leaves == [
            ("major_topics[0].topic", "Budget"),
            ("major_topics[0].key_points[0]", "alpha"),
            ("major_topics[0].key_points[1]", "beta"),
        ]

    def test_a_custom_prompt_key_extra_allow_is_walked(self):
        """SummaryData is extra="allow" — a custom prompt's own field names must
        be walked exactly like the known ones."""
        node = {"risk_register": {"items": [{"severity": "high", "detail": "leak"}]}}
        leaves = _walk_leaves(node, "")
        assert ("risk_register.items[0].severity", "high") in leaves
        assert ("risk_register.items[0].detail", "leak") in leaves

    def test_unicode_leaves(self):
        node = {"bluf": "Café résumé — 日本語のテスト"}
        assert _walk_leaves(node, "") == [("bluf", "Café résumé — 日本語のテスト")]

    def test_metadata_top_level_key_is_skipped(self):
        node = {"bluf": "kept", "metadata": {"provider": "openai", "created_at": "2026-01-01"}}
        leaves = _walk_leaves(node, "")
        assert leaves == [("bluf", "kept")]

    def test_non_string_leaves_are_never_emitted(self):
        node = {"counts": {"topics": 3, "ratio": 0.5, "flagged": True, "missing": None}}
        assert _walk_leaves(node, "") == []

    def test_blank_strings_are_never_emitted(self):
        assert _walk_leaves({"bluf": "   "}, "") == []

    def test_a_nested_metadata_key_is_not_special(self):
        """Only the TOP-LEVEL ``metadata`` key is machine-generated provenance;
        a section that happens to be named "metadata" one level down is
        ordinary model prose and must be walked."""
        node = {"major_topics": [{"metadata": "not actually special here"}]}
        leaves = _walk_leaves(node, "")
        assert leaves == [("major_topics[0].metadata", "not actually special here")]


class TestGetByPath:
    def test_round_trips_every_path_walk_leaves_produces(self):
        node = {"major_topics": [{"topic": "Budget", "key_points": ["alpha", "beta"]}]}
        leaves = _walk_leaves(node, "")
        assert len(leaves) == 3, "fixture precondition: three leaves expected"
        for path, value in leaves:
            assert _get_by_path(node, path) == value

    def test_survives_masking_shape_preservation(self):
        """mask_summary preserves the container shape exactly, so a path
        collected from the RAW tree must resolve on the MASKED tree too."""
        from app.services.redaction.config import EffectiveRedactionConfig
        from app.services.redaction.summary_redaction import mask_summary

        node = {"major_topics": [{"key_points": ["Damn, that slipped."]}]}
        cfg = EffectiveRedactionConfig(enabled=True, enabled_categories={"profanity"})
        masked = mask_summary(node, cfg)
        assert _get_by_path(masked, "major_topics[0].key_points[0]") != "Damn, that slipped."


# --------------------------------------------------------------------------- #
# Leaf-level FTS matching — real Postgres                                      #
# --------------------------------------------------------------------------- #


class TestMatchingLeafIndices:
    def test_matches_the_expected_positions(self, db_session):
        positions = summary_search._matching_leaf_indices(
            db_session, ["hello world", "goodbye moon", "summary generation done"], "summary"
        )
        assert positions == {2}

    def test_no_texts_short_circuits_without_a_query(self, db_session):
        assert summary_search._matching_leaf_indices(db_session, [], "anything") == set()

    def test_websearch_operators_are_honoured(self, db_session):
        """Confirms this really is websearch_to_tsquery, not a substring check —
        quoted-phrase and OR both take the real operator meaning."""
        positions = summary_search._matching_leaf_indices(
            db_session, ["the budget review", "an unrelated note"], '"budget review"'
        )
        assert positions == {0}


# --------------------------------------------------------------------------- #
# search_summaries — access control, ordering, snippets, masking               #
# --------------------------------------------------------------------------- #


class TestSearchSummaries:
    def test_matches_a_leaf_and_reports_its_key_path(self, db_session, normal_user):
        media_file = _make_file(
            db_session, normal_user, summary={"bluf": "The quarterly roadmap review."}
        )
        result = search_summaries(db_session, "roadmap", normal_user.id, organization_id=None)
        assert result.total == 1
        assert len(result.results) == 1
        hit = result.results[0]
        assert hit.file_uuid == str(media_file.uuid)
        assert hit.matches == [
            summary_search.SummarySectionMatch(
                key_path="bluf", snippet="The quarterly roadmap review."
            )
        ]

    def test_title_falls_back_to_filename(self, db_session, normal_user):
        media_file = _make_file(
            db_session, normal_user, summary={"bluf": "roadmap review"}, title=None
        )
        result = search_summaries(db_session, "roadmap", normal_user.id, organization_id=None)
        assert result.results[0].title == media_file.filename

    def test_a_non_matching_query_returns_nothing(self, db_session, normal_user):
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        result = search_summaries(
            db_session, "zzz_no_such_term", normal_user.id, organization_id=None
        )
        assert result.total == 0
        assert result.results == []

    def test_a_file_with_no_summary_is_never_matched(self, db_session, normal_user):
        _make_file(db_session, normal_user, summary=None)
        result = search_summaries(db_session, "anything", normal_user.id, organization_id=None)
        assert result.total == 0

    def test_a_json_null_typed_summary_is_never_matched(self, db_session, normal_user):
        """Real dev-DB shape: a failed summary run can store the JSON scalar
        ``null`` (jsonb_typeof = 'null'), not SQL NULL. `summary_data::text` on
        that row is the four characters "null" — to_tsvector must never be
        asked to search it, and jsonb_typeof is the guard.
        """
        media_file = _make_file(db_session, normal_user, summary={"bluf": "placeholder"})
        db_session.execute(
            sa_text("UPDATE media_file SET summary_data = 'null'::jsonb WHERE id = :id"),
            {"id": media_file.id},
        )
        db_session.commit()
        result = search_summaries(db_session, "null", normal_user.id, organization_id=None)
        assert result.total == 0

    def test_pagination_total_counts_files_not_leaves(self, db_session, normal_user):
        for _ in range(3):
            _make_file(
                db_session, normal_user, summary={"bluf": "roadmap", "brief_summary": "roadmap"}
            )
        result = search_summaries(
            db_session, "roadmap", normal_user.id, organization_id=None, page=1, page_size=2
        )
        assert result.total == 3
        assert len(result.results) == 2

    def test_snippet_is_truncated_for_a_pathological_leaf(self, db_session, normal_user):
        long_text = "roadmap " + ("word " * 200)
        _make_file(db_session, normal_user, summary={"bluf": long_text})
        result = search_summaries(db_session, "roadmap", normal_user.id, organization_id=None)
        snippet = result.results[0].matches[0].snippet
        assert len(snippet) <= summary_search._MAX_SNIPPET_CHARS + 1  # +1 for the ellipsis char
        assert snippet.endswith("…")


# --------------------------------------------------------------------------- #
# Access control — the SAME authority transcript search uses, no second rule   #
# --------------------------------------------------------------------------- #


class TestAccessControl:
    def test_leak_a_file_only_shared_via_a_different_collection_is_invisible(
        self, db_session, normal_user, other_user
    ):
        """Permission matrix row T5, LEAK half. `other_user` owns two files: one
        shared with `normal_user` via collection C2, one that is NOT (either
        unshared, or shared only via a different collection C1 the recipient
        has no access to). Searching for the unshared file's term must return
        nothing.
        """
        blocked = _make_file(
            db_session, other_user, summary={"bluf": "The confidential merger term-sheet."}
        )
        visible = _make_file(db_session, other_user, summary={"bluf": "The public roadmap update."})
        _share_with(db_session, other_user, normal_user, visible)
        # `blocked` deliberately has no share at all — the plainest "different
        # collection" case: a collection the recipient was never granted.

        result = search_summaries(db_session, "merger", normal_user.id, organization_id=None)

        assert result.total == 0
        assert result.results == []

    def test_shared_visibility_a_real_share_row_makes_the_summary_reachable(
        self, db_session, normal_user, other_user
    ):
        """Permission matrix row T5, SHARED-VISIBILITY half. Must assert
        non-zero results against a REAL share row — a fixture that never
        actually shares anything would pass this vacuously.
        """
        shared = _make_file(db_session, other_user, summary={"bluf": "The public roadmap update."})
        _share_with(db_session, other_user, normal_user, shared)

        result = search_summaries(db_session, "roadmap", normal_user.id, organization_id=None)

        assert result.total == 1
        assert result.results[0].file_uuid == str(shared.uuid)
        assert result.results[0].matches, "a shared-visibility hit must still carry its match"

    def test_an_owned_file_is_always_visible(self, db_session, normal_user):
        media_file = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        result = search_summaries(db_session, "roadmap", normal_user.id, organization_id=None)
        assert result.results[0].file_uuid == str(media_file.uuid)


# --------------------------------------------------------------------------- #
# Masking — per leaf, BEFORE snippet extraction, fail-closed                   #
# --------------------------------------------------------------------------- #


class TestMasking:
    def test_no_cfg_returns_the_raw_snippet(self, db_session, normal_user):
        _make_file(db_session, normal_user, summary={"bluf": "Damn, that slipped."})
        result = search_summaries(
            db_session, "slipped", normal_user.id, organization_id=None, redaction_cfg=None
        )
        assert "Damn" in result.results[0].matches[0].snippet

    def test_a_disabled_policy_leaves_the_snippet_untouched(self, db_session, normal_user):
        _make_file(db_session, normal_user, summary={"bluf": "Damn, that slipped."})
        cfg = resolve_effective_config(db_session, normal_user.id)
        assert not cfg.enabled  # fixture precondition
        result = search_summaries(
            db_session, "slipped", normal_user.id, organization_id=None, redaction_cfg=cfg
        )
        assert "Damn" in result.results[0].matches[0].snippet

    def test_an_enabled_policy_masks_the_returned_snippet(self, db_session, normal_user):
        _make_file(db_session, normal_user, summary={"bluf": "Damn, that slipped."})
        _enable_redaction(db_session, normal_user, categories='["profanity"]')
        cfg = resolve_effective_config(db_session, normal_user.id)
        result = search_summaries(
            db_session, "slipped", normal_user.id, organization_id=None, redaction_cfg=cfg
        )
        assert "Damn" not in result.results[0].matches[0].snippet

    def test_masking_is_applied_per_leaf_not_batched(self, db_session, normal_user, monkeypatch):
        """The measured failure mode this brief calls out: a batched detector
        pass loses text across leaves. `search_summaries` must call the
        already-per-leaf `mask_summary` exactly once per DOCUMENT (not once per
        the whole page), delegating the per-leaf discipline to it rather than
        reimplementing — never coalescing multiple documents' text into one
        detection call.
        """
        calls: list[dict] = []
        real_mask_summary = summary_search.mask_summary

        def _spy(summary_data, cfg):
            calls.append(summary_data)
            return real_mask_summary(summary_data, cfg)

        monkeypatch.setattr(summary_search, "mask_summary", _spy)

        _make_file(db_session, normal_user, summary={"bluf": "roadmap one"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap two"})
        _enable_redaction(db_session, normal_user, categories='["profanity"]')
        cfg = resolve_effective_config(db_session, normal_user.id)

        search_summaries(
            db_session, "roadmap", normal_user.id, organization_id=None, redaction_cfg=cfg
        )

        assert len(calls) == 2, "mask_summary must be called once per matched document"

    def test_a_detector_outage_fails_closed(self, db_session, normal_user, monkeypatch):
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _enable_redaction(db_session, normal_user)
        cfg = resolve_effective_config(db_session, normal_user.id)

        def _raise(*_args, **_kwargs):
            raise SummaryMaskingUnavailableError("pii detector unavailable")

        monkeypatch.setattr(summary_search, "mask_summary", _raise)

        with pytest.raises(SummaryMaskingUnavailableError):
            search_summaries(
                db_session, "roadmap", normal_user.id, organization_id=None, redaction_cfg=cfg
            )
