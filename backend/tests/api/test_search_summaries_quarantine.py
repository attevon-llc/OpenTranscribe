"""``GET /api/search?result_type=summaries`` must not be a content oracle (#818).

Sibling of ``tests/api/test_search_count_quarantine.py``. Before the fix,
``_summary_search_payload`` POST-filtered the hit list after ``search_summaries`` had
already computed ``total`` over the whole (unfiltered) result set, so a taken-down
file whose hit sorted onto a later page still inflated ``summary_total`` — the same
content-oracle class #876 closed for ``/search/count``. The fix moved the quarantine
predicate INSIDE ``search_summaries`` as a pre-filter (``exclude_quarantined``), so
``summary_total`` and ``total_pages`` are now computed over what the caller can
actually see.

OpenSearch is stubbed out entirely (``transcript_search_must_not_be_called``,
same technique ``test_search_result_type.py`` uses) — this is a pure-Postgres leg.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.services.search.hybrid_search_service import HybridSearchService

SEARCH_PATH = "/api/search"


@pytest.fixture
def transcript_search_must_not_be_called(monkeypatch):
    def _explode(_self, **_kwargs):
        raise AssertionError("HybridSearchService.search was called for a summaries-only request")

    monkeypatch.setattr(HybridSearchService, "search", _explode)


def _make_file(db_session, user, *, summary: dict) -> MediaFile:
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


def test_summary_total_does_not_disclose_an_off_page_taken_down_file(
    client,
    user_token_headers,
    normal_user,
    db_session,
    transcript_search_must_not_be_called,
):
    # Created FIRST -> lower id -> sorts onto page 2 under `MediaFile.id.desc()`.
    quarantined = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
    clean = _make_file(db_session, normal_user, summary={"bluf": "roadmap review too"})
    quarantined.is_quarantined = True
    db_session.commit()

    body = client.get(
        SEARCH_PATH,
        params={"q": "roadmap", "result_type": "summaries", "page": 1, "page_size": 1},
        headers=user_token_headers,
    ).json()

    assert body["summary_total"] == 1, body
    assert body["total_pages"] == 1, body
    assert [h["file_uuid"] for h in body["summary_results"]] == [str(clean.uuid)]


def test_a_visible_summary_is_still_returned_with_its_snippet(
    client,
    user_token_headers,
    normal_user,
    db_session,
    transcript_search_must_not_be_called,
):
    """CONTROL: an ordinary summary must still come back, non-empty."""
    media_file = _make_file(
        db_session, normal_user, summary={"bluf": "a distinctive roadmap update"}
    )

    body = client.get(
        SEARCH_PATH,
        params={"q": "roadmap", "result_type": "summaries"},
        headers=user_token_headers,
    ).json()

    assert body["summary_total"] == 1, body
    assert len(body["summary_results"]) == 1
    hit = body["summary_results"][0]
    assert hit["file_uuid"] == str(media_file.uuid)
    assert hit["matches"], "a matching summary must carry at least one snippet"


def test_an_admin_still_sees_both(
    client,
    admin_token_headers,
    admin_user,
    db_session,
    transcript_search_must_not_be_called,
):
    clean = _make_file(db_session, admin_user, summary={"bluf": "roadmap review"})
    quarantined = _make_file(db_session, admin_user, summary={"bluf": "roadmap review too"})
    quarantined.is_quarantined = True
    db_session.commit()

    body = client.get(
        SEARCH_PATH,
        params={"q": "roadmap", "result_type": "summaries"},
        headers=admin_token_headers,
    ).json()

    assert body["summary_total"] == 2, body
    uuids = {h["file_uuid"] for h in body["summary_results"]}
    assert uuids == {str(clean.uuid), str(quarantined.uuid)}
