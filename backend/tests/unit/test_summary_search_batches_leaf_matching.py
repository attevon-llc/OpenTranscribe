"""Issue #822 unit 4: leaf-level FTS matching batched across the whole result
PAGE, not issued once per file.

Before this, `search_summaries` called `_matching_leaf_indices` once per row
in the page — a small, real cost on top of the masking narrowing Unit 3
closed. `_matching_leaf_indices` now takes three parallel arrays (row index,
leaf index, leaf text) for the WHOLE page and issues one `unnest(...)` query;
the loop that used to call it per-file instead partitions its single result
set back onto each row.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.models.media import MediaFile
from app.services.search.summary_search import search_summaries

pytestmark = pytest.mark.unit


def _make_file(db_session, user, *, summary) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
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


def _count_leaf_matching_statements(monkeypatch):
    """Spy on `Session.execute`, counting only the leaf-matching statement —
    identified by the `leaf_text` unnest alias, which is distinctive to
    `_matching_leaf_indices` and appears in no other query this module issues.
    """
    real_execute = OrmSession.execute
    calls: list[str] = []

    def _spy(self, statement, *args, **kwargs):
        if "leaf_text" in str(statement):
            calls.append(str(statement))
        return real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(OrmSession, "execute", _spy)
    return calls


class TestOneQueryPerPage:
    def test_one_leaf_query_per_page_not_one_per_file(self, db_session, normal_user, monkeypatch):
        for i in range(3):
            _make_file(db_session, normal_user, summary={"bluf": f"roadmap review {i}"})

        calls = _count_leaf_matching_statements(monkeypatch)

        result = search_summaries(db_session, "roadmap", normal_user.id, organization_id=None)

        assert result.total == 3
        assert len(result.results) == 3
        assert len(calls) == 1, (
            "expected exactly one batched leaf-matching query for the whole "
            f"3-file page, got {len(calls)}"
        )


class TestAttribution:
    def test_leaf_matches_are_attributed_to_the_right_file(self, db_session, normal_user):
        """Two files share the matching term but differ in WHICH section
        matches. Without this, an off-by-one in the parallel row/leaf arrays
        cross-attributes a snippet from one file's leaf onto the other's hit —
        the one way this refactor can leak. The attribution test PASSES on
        HEAD too (a per-row loop cannot cross-attribute); it is a
        non-regression guard for this unit, not evidence the batching itself
        works — see `test_one_leaf_query_per_page_not_one_per_file` for that.
        """
        file_a = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "the budget review", "notes": "an unrelated passage"},
        )
        file_b = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "an unrelated passage", "notes": "the budget report"},
        )

        result = search_summaries(db_session, "budget", normal_user.id, organization_id=None)

        assert result.total == 2
        by_uuid = {hit.file_uuid: hit for hit in result.results}
        assert set(by_uuid) == {str(file_a.uuid), str(file_b.uuid)}

        a_paths = {m.key_path for m in by_uuid[str(file_a.uuid)].matches}
        b_paths = {m.key_path for m in by_uuid[str(file_b.uuid)].matches}
        assert a_paths == {"bluf"}, "file A's match must be its own 'bluf' leaf, not file B's"
        assert b_paths == {"notes"}, "file B's match must be its own 'notes' leaf, not file A's"
