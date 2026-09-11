"""GET /api/search's ``result_type`` parameter (issue #462).

One parameter, ``result_type: transcripts | summaries | all``, defaulting to
``transcripts`` so every existing caller is byte-identical.

Most of this needs no OpenSearch: the transcript leg is stubbed via
``HybridSearchService.search`` (same technique as ``test_search.py``), so these
tests are deterministic and fast, and the summary leg is real Postgres.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag
from app.models.prompt import UserSetting
from app.models.sharing import CollectionShare
from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import SearchHit
from app.services.search.hybrid_search_service import SearchResponse

SEARCH_PATH = "/api/search"

_DEFAULT_KEYS = {
    "query",
    "results",
    "total_results",
    "total_files",
    "page",
    "page_size",
    "total_pages",
    "search_time_ms",
    "filters_applied",
    "search_mode",
}


def _empty_transcript_response(query: str) -> SearchResponse:
    return SearchResponse(
        query=query,
        results=[],
        total_results=0,
        total_files=0,
        page=1,
        page_size=20,
        total_pages=0,
        search_time_ms=1.0,
    )


@pytest.fixture
def stub_transcript_search(monkeypatch):
    """Deterministic, empty transcript leg — installed explicitly per test so a
    test that means to prove the transcript leg is NEVER called can instead
    install a raising stub."""

    def _fake_search(_self, **kwargs):
        return _empty_transcript_response(kwargs.get("query", ""))

    monkeypatch.setattr(HybridSearchService, "search", _fake_search)


@pytest.fixture
def transcript_search_must_not_be_called(monkeypatch):
    def _explode(_self, **_kwargs):
        raise AssertionError("HybridSearchService.search was called for a summaries-only request")

    monkeypatch.setattr(HybridSearchService, "search", _explode)


def _transcript_hit(file_uuid: str) -> SearchHit:
    return SearchHit(
        file_uuid=file_uuid,
        file_id=abs(hash(file_uuid)) % 1_000_000,
        title=file_uuid,
        speakers=[],
        tags=[],
        upload_time="2026-03-10T00:00:00+00:00",
        language="en",
    )


@pytest.fixture
def paginating_transcript_leg(monkeypatch):
    """A transcript leg that really paginates, over a caller-chosen hit count.

    The `all` pagination tests (issue #831 item 3) are about the two legs having
    DIFFERENT page counts, so the transcript leg's own page count has to be
    controllable and its slices real — an unconditionally-empty stub reports
    ``total_pages`` 1 forever and could never show the defect.

    Returns an installer taking the number of transcript hits to serve; it
    returns the uuids in the order the leg will hand them out.
    """

    def _install(hit_count: int) -> list[str]:
        uuids = [str(uuid_pkg.uuid4()) for _ in range(hit_count)]

        def _fake_search(_self, **kwargs):
            page = kwargs.get("page", 1)
            page_size = kwargs.get("page_size", 20)
            start = (page - 1) * page_size
            window = uuids[start : start + page_size]
            return SearchResponse(
                query=kwargs.get("query", ""),
                results=[_transcript_hit(u) for u in window],
                total_results=len(uuids),
                total_files=len(uuids),
                page=page,
                page_size=page_size,
                # The real service's formula, `max(1, ceil(...))` — including the
                # floor of 1 on an empty result, which is what made an `all`
                # request with no transcript hits cap the whole response at one
                # page.
                total_pages=max(1, (len(uuids) + page_size - 1) // page_size),
                search_time_ms=1.0,
            )

        monkeypatch.setattr(HybridSearchService, "search", _fake_search)
        return uuids

    return _install


def _make_file(
    db_session,
    user,
    *,
    summary,
    title=None,
    content_type="audio/wav",
    file_size=1024,
    duration=None,
    language=None,
    upload_time=None,
    creation_date=None,
) -> MediaFile:
    """A completed, summarized file owned by ``user``.

    The metadata columns are keyword-only with defaults so the filter tests
    (issue #831) can pin one dimension at a time without every older test in
    this module having to name them.
    """
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        title=title,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type=content_type,
        file_size=file_size,
        user_id=user.id,
        status="completed",
        summary_data=summary,
    )
    # Assigned only when asked for: ``upload_time`` carries a server default, and
    # writing an explicit None would replace "now" with NULL.
    if duration is not None:
        row.duration = duration
    if language is not None:
        row.language = language
    if upload_time is not None:
        row.upload_time = upload_time
    if creation_date is not None:
        row.creation_date = creation_date
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _tag_file(db_session, user, media_file, name: str) -> None:
    """Attach an owned tag named ``name`` to ``media_file``.

    The tag row is reused when the owner already has one by that name: a `Tag`
    is per-owner and `uq_tag_user_name` makes a second row an IntegrityError, so
    tagging two files with the same word — which is the whole point of a tag
    filter test — has to go through the same resolve-then-attach the product
    does (``tag_service.resolve_or_create_tag``).
    """
    tag = db_session.query(Tag).filter(Tag.user_id == user.id, Tag.name == name).first()
    if tag is None:
        tag = Tag(name=name, user_id=user.id)
        db_session.add(tag)
        db_session.commit()
    db_session.add(FileTag(media_file_id=media_file.id, tag_id=tag.id))
    db_session.commit()


def _collect_file(db_session, user, media_file, name: str) -> Collection:
    """Put ``media_file`` in a new collection owned by ``user``."""
    collection = Collection(user_id=user.id, name=name, description="filter test")
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.commit()
    return collection


def _add_speaker(db_session, user, media_file, *, name: str, display_name=None) -> Speaker:
    speaker = Speaker(
        user_id=user.id,
        media_file_id=media_file.id,
        name=name,
        display_name=display_name,
    )
    db_session.add(speaker)
    db_session.commit()
    return speaker


def _summary_search(client, headers, q: str, **params):
    """Run a summaries-only search and return ``(summary_total, [file_uuid, …])``.

    Returning both is the point: issue #831's count-consistency requirement is
    about them agreeing, so no test here may read one without the other being
    available to it.
    """
    response = client.get(
        SEARCH_PATH,
        params={"q": q, "result_type": "summaries", **params},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["summary_total"], [hit["file_uuid"] for hit in body["summary_results"]]


def _share_with(db_session, owner, recipient, media_file, *, permission="viewer") -> None:
    collection = Collection(
        user_id=owner.id,
        name=f"share-{uuid_pkg.uuid4().hex[:8]}",
        description="result_type endpoint test",
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
# Parameter contract                                                           #
# --------------------------------------------------------------------------- #


class TestResultTypeParameterContract:
    def test_an_unknown_result_type_is_400(
        self, client, user_token_headers, stub_transcript_search
    ):
        response = client.get(
            SEARCH_PATH, params={"q": "test", "result_type": "bogus"}, headers=user_token_headers
        )
        assert response.status_code == 400, response.text


class TestDefaultIsByteIdenticalToTranscriptsOnly:
    def test_omitting_result_type_matches_explicit_transcripts(
        self, client, user_token_headers, stub_transcript_search
    ):
        omitted = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers)
        explicit = client.get(
            SEARCH_PATH,
            params={"q": "test", "result_type": "transcripts"},
            headers=user_token_headers,
        )
        assert omitted.status_code == 200, omitted.text
        assert explicit.status_code == 200, explicit.text
        assert omitted.json() == explicit.json()

    def test_the_default_response_carries_no_summary_keys(
        self, client, user_token_headers, stub_transcript_search
    ):
        body = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers).json()
        # `embedding_warning` is pre-existing, conditional (#437 mixed-index
        # advisory) and unrelated to this change — everything else must match
        # the documented transcript-only shape exactly.
        assert set(body.keys()) - {"embedding_warning"} == _DEFAULT_KEYS
        assert "summary_results" not in body
        assert "summary_total" not in body

    def test_a_summary_that_would_match_is_invisible_by_default(
        self, client, user_token_headers, normal_user, db_session, stub_transcript_search
    ):
        """The strongest form of the pin: a summary genuinely matching the
        query must not leak into the default (transcripts-only) response."""
        _make_file(db_session, normal_user, summary={"bluf": "a very particular roadmap phrase"})
        body = client.get(
            SEARCH_PATH, params={"q": "particular"}, headers=user_token_headers
        ).json()
        assert body["results"] == []
        assert "summary_results" not in body


# --------------------------------------------------------------------------- #
# summaries                                                                     #
# --------------------------------------------------------------------------- #


class TestSummariesResultType:
    def test_summaries_only_never_calls_the_transcript_service(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text

    def test_summaries_only_response_shape(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        _make_file(db_session, normal_user, summary={"bluf": "a distinctive roadmap phrase"})
        body = client.get(
            SEARCH_PATH,
            params={"q": "distinctive", "result_type": "summaries"},
            headers=user_token_headers,
        ).json()
        assert body["results"] == []
        assert body["total_results"] == 0
        assert body["summary_total"] == 1
        assert len(body["summary_results"]) == 1
        hit = body["summary_results"][0]
        assert hit["matches"] == [{"key_path": "bluf", "snippet": "a distinctive roadmap phrase"}]

    def test_all_returns_both_legs(
        self, client, user_token_headers, normal_user, db_session, stub_transcript_search
    ):
        _make_file(db_session, normal_user, summary={"bluf": "a distinctive roadmap phrase"})
        body = client.get(
            SEARCH_PATH,
            params={"q": "distinctive", "result_type": "all"},
            headers=user_token_headers,
        ).json()
        assert "results" in body  # transcript leg (stubbed empty)
        assert body["summary_total"] == 1


class TestSummaryMaskingFailsClosed:
    def test_a_detector_outage_on_a_returned_leaf_is_503(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        monkeypatch,
        stub_transcript_search,
    ):
        """A detector outage on a leaf that IS about to be disclosed must still
        withhold the result — the narrowing (issue #822) only removes the false
        positive for leaves that were never going to be shown, see the sibling
        test below."""
        from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
        from app.services.search import summary_search

        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        for key, value in (("redaction_enabled", "true"), ("redaction_categories", '["pii"]')):
            db_session.add(
                UserSetting(user_id=normal_user.id, setting_key=key, setting_value=value)
            )
        db_session.commit()

        def _raise(*_args, **_kwargs):
            raise SummaryMaskingUnavailableError("pii detector unavailable")

        monkeypatch.setattr(summary_search, "mask_summary_leaf", _raise)

        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 503, response.text

    def test_a_detector_outage_is_scoped_to_the_leaves_actually_returned(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        monkeypatch,
        stub_transcript_search,
    ):
        """The real #822 fix, proven rather than assumed: a summary with a
        matching leaf AND a non-matching leaf, where the masker raises only for
        the non-matching (never-returned) text, must still answer 200. Before
        the fix the whole tree was masked up front, so this same setup 503'd —
        the outage was never actually about the leaf being shown."""
        from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
        from app.services.search import summary_search

        _make_file(
            db_session,
            normal_user,
            summary={
                "bluf": "roadmap review",
                "other_field": "an unrelated section about something else entirely",
            },
        )
        for key, value in (("redaction_enabled", "true"), ("redaction_categories", '["pii"]')):
            db_session.add(
                UserSetting(user_id=normal_user.id, setting_key=key, setting_value=value)
            )
        db_session.commit()

        real_mask_summary_leaf = summary_search.mask_summary_leaf

        def _raise_only_for_the_non_matching_leaf(text, cfg):
            if "roadmap" in text:
                return real_mask_summary_leaf(text, cfg)
            raise SummaryMaskingUnavailableError("pii detector unavailable")

        monkeypatch.setattr(
            summary_search, "mask_summary_leaf", _raise_only_for_the_non_matching_leaf
        )

        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary_total"] == 1
        assert body["summary_results"][0]["matches"] == [
            {"key_path": "bluf", "snippet": "roadmap review"}
        ]


class TestPermissionMatrixT5:
    def test_leak_a_summary_shared_only_via_a_different_collection_is_invisible(
        self,
        client,
        user_token_headers,
        normal_user,
        other_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        blocked = _make_file(
            db_session, other_user, summary={"bluf": "The confidential merger term-sheet."}
        )
        visible = _make_file(db_session, other_user, summary={"bluf": "The public roadmap update."})
        _share_with(db_session, other_user, normal_user, visible)

        response = client.get(
            SEARCH_PATH,
            params={"q": "merger", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary_total"] == 0
        assert body["summary_results"] == []
        assert str(blocked.uuid) not in [h["file_uuid"] for h in body["summary_results"]]

    def test_shared_visibility_a_real_share_makes_the_summary_reachable(
        self,
        client,
        user_token_headers,
        normal_user,
        other_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        shared = _make_file(db_session, other_user, summary={"bluf": "The public roadmap update."})
        _share_with(db_session, other_user, normal_user, shared)

        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary_total"] == 1
        assert [h["file_uuid"] for h in body["summary_results"]] == [str(shared.uuid)]
        assert body["summary_results"][0]["matches"], "a shared-visibility hit must carry its match"


# --------------------------------------------------------------------------- #
# Quarantine (DMCA/abuse) parity with the transcript leg                       #
# --------------------------------------------------------------------------- #


class TestQuarantinedSummaryHitsAreDropped:
    def test_a_quarantined_files_summary_is_dropped_for_a_non_admin(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        media_file = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        media_file.is_quarantined = True
        db_session.commit()

        body = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        ).json()
        assert body["summary_total"] == 0
        assert body["summary_results"] == []

    def test_an_admin_still_sees_it(
        self,
        client,
        admin_token_headers,
        admin_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        media_file = _make_file(db_session, admin_user, summary={"bluf": "roadmap review"})
        media_file.is_quarantined = True
        db_session.commit()

        body = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=admin_token_headers,
        ).json()
        assert body["summary_total"] == 1
        assert [h["file_uuid"] for h in body["summary_results"]] == [str(media_file.uuid)]


# --------------------------------------------------------------------------- #
# Metadata filters (issue #831 item 1)                                          #
#                                                                               #
# The SPA sends every filter on every tab, so a filter the transcript leg       #
# honours and the summary leg drops means one request's two legs disagree about #
# which files the caller asked for. Each test below pairs a file that should    #
# survive the filter with one that should not: the survivor is the control      #
# (the query itself still works), the excluded one is the claim.                #
# --------------------------------------------------------------------------- #


class TestSummaryLegHonoursTheDateRange:
    def test_a_summary_outside_the_date_range_is_excluded(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        inside = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            upload_time=datetime(2026, 3, 10, tzinfo=UTC),
        )
        _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            upload_time=datetime(2026, 5, 10, tzinfo=UTC),
        )

        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", date_from="2026-03-01", date_to="2026-03-31"
        )
        assert uuids == [str(inside.uuid)]
        assert total == 1

    def test_a_date_only_upper_bound_covers_the_whole_day(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """OpenSearch rounds an ``lte`` bound UP to its granularity, so the
        transcript leg's ``date_to=2026-03-10`` includes everything recorded that
        day. Read as a bare midnight here, a file uploaded at 14:00 would be
        dropped from the summary leg alone — the two legs of one request
        disagreeing about an entire day.
        """
        afternoon = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            upload_time=datetime(2026, 3, 10, 14, 0, tzinfo=UTC),
        )
        # The next morning — outside the bound, so the bound is proved to be
        # applied at all rather than merely generous.
        _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            upload_time=datetime(2026, 3, 11, 9, 0, tzinfo=UTC),
        )

        total, uuids = _summary_search(client, user_token_headers, "roadmap", date_to="2026-03-10")
        assert uuids == [str(afternoon.uuid)]
        assert total == 1

    def test_the_range_reads_creation_date_ahead_of_upload_time(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """``search_indexing_task`` writes ``creation_date or upload_time`` into
        the index's ``upload_time`` field, so the transcript leg filters on the
        container's claimed date when there is one. Filtering the summary leg on
        ``upload_time`` alone would put the same file on opposite sides of the
        same bound.
        """
        recorded_in_march = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            upload_time=datetime(2026, 5, 10, tzinfo=UTC),
            creation_date=datetime(2026, 3, 10, tzinfo=UTC),
        )
        # The mirror image: uploaded inside the range, RECORDED outside it. If
        # the predicate read `upload_time` this file would be kept and the one
        # above dropped — the two assertions below then fail in opposite
        # directions, so neither column can satisfy this test by accident.
        _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            upload_time=datetime(2026, 3, 20, tzinfo=UTC),
            creation_date=datetime(2026, 5, 20, tzinfo=UTC),
        )

        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", date_from="2026-03-01", date_to="2026-03-31"
        )
        assert uuids == [str(recorded_in_march.uuid)]
        assert total == 1

    def test_an_unparseable_date_is_a_400_not_a_silently_dropped_bound(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """A bound that cannot be parsed must never be dropped: the caller would
        get an unfiltered page while believing they had filtered, which is the
        whole defect class this lane closes.
        """
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})

        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries", "date_from": "last-tuesday"},
            headers=user_token_headers,
        )
        assert response.status_code == 400, response.text


class TestSummaryLegHonoursTagsAndCollections:
    def test_an_untagged_summary_is_excluded_by_a_tag_filter(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        tagged = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _tag_file(db_session, normal_user, tagged, "quarterly")

        total, uuids = _summary_search(client, user_token_headers, "roadmap", tags=["quarterly"])
        assert uuids == [str(tagged.uuid)]
        assert total == 1

    def test_several_tags_match_any_of_them_like_the_transcript_leg(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """The index filters tags with a ``terms`` clause, which is OR. The
        gallery's helper is AND (``HAVING COUNT(...) == len(tag)``); reusing it
        here would have made a two-tag search return nothing on the Summaries tab
        while the Transcripts tab returned both files.
        """
        one = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        two = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _tag_file(db_session, normal_user, one, "quarterly")
        _tag_file(db_session, normal_user, two, "retro")

        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", tags=["quarterly", "retro"]
        )
        assert total == 2
        assert set(uuids) == {str(one.uuid), str(two.uuid)}

    def test_a_summary_outside_the_collection_is_excluded(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        member = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        collection = _collect_file(db_session, normal_user, member, "Q1 planning")

        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", collection_id=collection.id
        )
        assert uuids == [str(member.uuid)]
        assert total == 1


class TestSummaryLegHonoursTheFileMetadataFilters:
    def test_a_summary_whose_file_has_no_matching_speaker_is_excluded(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        with_dana = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        without_dana = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _add_speaker(db_session, normal_user, with_dana, name="SPEAKER_00", display_name="Dana")
        _add_speaker(db_session, normal_user, without_dana, name="SPEAKER_00", display_name="Rui")

        total, uuids = _summary_search(client, user_token_headers, "roadmap", speakers=["Dana"])
        assert uuids == [str(with_dana.uuid)]
        assert total == 1

    def test_the_file_type_filter_matches_the_mime_family_not_the_literal_word(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """``content_type`` holds a full MIME type, so comparing it to the literal
        string ``"video"`` matches nothing — the defect #871 fixed on the
        transcript leg. This pins the prefix semantics rather than merely that
        *something* was excluded.
        """
        video = _make_file(
            db_session, normal_user, summary={"bluf": "roadmap review"}, content_type="video/mp4"
        )
        _make_file(
            db_session, normal_user, summary={"bluf": "roadmap review"}, content_type="audio/wav"
        )

        total, uuids = _summary_search(client, user_token_headers, "roadmap", file_type=["video"])
        assert uuids == [str(video.uuid)]
        assert total == 1

    def test_a_summary_in_another_language_is_excluded(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        spanish = _make_file(
            db_session, normal_user, summary={"bluf": "roadmap review"}, language="es"
        )
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"}, language="en")

        total, uuids = _summary_search(client, user_token_headers, "roadmap", language="es")
        assert uuids == [str(spanish.uuid)]
        assert total == 1

    def test_a_file_with_no_detected_language_still_answers_to_en(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """``search_indexing_task`` indexes ``media_file.language or "en"``, so a
        file that never got a language IS matched by ``?language=en`` on the
        transcript leg. Comparing the raw column here would have dropped it from
        the summary leg only.
        """
        undetected = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"}, language="es")

        total, uuids = _summary_search(client, user_token_headers, "roadmap", language="en")
        assert uuids == [str(undetected.uuid)]
        assert total == 1

    def test_the_title_filter_excludes_a_non_matching_title(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        planning = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            title="Quarterly planning",
        )
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"}, title="Ad hoc sync")

        # Lower-cased on purpose: the index's wildcard is case-insensitive.
        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", title_filter="quarterly"
        )
        assert uuids == [str(planning.uuid)]
        assert total == 1

    def test_a_too_short_recording_is_excluded_by_the_duration_range(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        long_file = _make_file(
            db_session, normal_user, summary={"bluf": "roadmap review"}, duration=600.0
        )
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"}, duration=60.0)

        total, uuids = _summary_search(client, user_token_headers, "roadmap", min_duration=300)
        assert uuids == [str(long_file.uuid)]
        assert total == 1

    def test_the_file_size_range_is_in_bytes_like_the_rest_of_this_endpoint(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """``/api/search`` documents these params as bytes and the SPA sends
        ``MB * 1024 * 1024``. The gallery's helper multiplies its argument by a
        megabyte; reusing it here would read this 10 MiB bound as 10 TiB and
        exclude everything, so this test pins the unit, not just the exclusion.
        """
        big = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            file_size=50 * 1024 * 1024,
        )
        _make_file(
            db_session,
            normal_user,
            summary={"bluf": "roadmap review"},
            file_size=5 * 1024 * 1024,
        )

        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", min_file_size=10 * 1024 * 1024
        )
        assert uuids == [str(big.uuid)]
        assert total == 1


class TestSummaryLegHonoursTheSingleFileScope:
    def test_file_uuid_scopes_the_summary_leg_to_one_file(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        wanted = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})

        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", file_uuid=str(wanted.uuid)
        )
        assert uuids == [str(wanted.uuid)]
        assert total == 1

    def test_an_unparseable_file_uuid_matches_nothing_rather_than_erroring(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """The transcript leg sends a junk value to OpenSearch as a term that
        matches nothing. Binding it to a ``uuid`` column raises instead, so the
        summary leg narrows to nothing explicitly — never falls through
        unfiltered, which would widen the scope the caller asked to narrow.
        """
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})

        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", file_uuid="not-a-uuid"
        )
        assert uuids == []
        assert total == 0


# --------------------------------------------------------------------------- #
# Relevance ranking (issue #831 item 2)                                         #
# --------------------------------------------------------------------------- #


class TestSummaryLegRanksByRelevance:
    def test_a_richer_summary_outranks_a_marginal_one(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """The relevant file is created FIRST on purpose: it therefore holds the
        LOWER id, so the previous ``ORDER BY id DESC`` put it second. A test that
        created it last would pass without any ranking at all.
        """
        relevant = _make_file(
            db_session,
            normal_user,
            summary={
                "bluf": "Budget, budget and more budget.",
                "brief_summary": "The budget review walked every budget line item.",
            },
        )
        marginal = _make_file(
            db_session,
            normal_user,
            summary={"bluf": "Mostly unrelated chatter, with one passing budget aside."},
        )

        total, uuids = _summary_search(client, user_token_headers, "budget")
        assert total == 2
        assert uuids == [str(relevant.uuid), str(marginal.uuid)]

    def test_equally_relevant_summaries_keep_a_stable_newest_first_order(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """CONTROL, and the paging invariant: ties are the common case on short
        summaries, and an ORDER BY that does not TOTALLY order the rows lets
        Postgres return them differently per page — duplicating some hits and
        dropping others across a paginated result set.
        """
        older = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        newer = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})

        total, uuids = _summary_search(client, user_token_headers, "roadmap")
        assert total == 2
        assert uuids == [str(newer.uuid), str(older.uuid)]


# --------------------------------------------------------------------------- #
# Count consistency — the #818-shaped regression                                #
# --------------------------------------------------------------------------- #


class TestFilteredCountMatchesWhatPagingReturns:
    def test_summary_total_counts_only_files_the_filters_kept(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """Every filter must be a PRE-filter inside the query that computes the
        count and takes the page offset (issue #818's rule). A filter applied to
        the returned hits instead leaves ``summary_total`` describing a larger
        set than the pages can ever yield — here, 3 instead of 2 — and
        ``total_pages`` promises a page that comes back empty.
        """
        kept_one = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        kept_two = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        excluded = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _tag_file(db_session, normal_user, kept_one, "quarterly")
        _tag_file(db_session, normal_user, kept_two, "quarterly")

        collected: list[str] = []
        for page in (1, 2):
            total, uuids = _summary_search(
                client,
                user_token_headers,
                "roadmap",
                tags=["quarterly"],
                page=page,
                page_size=1,
            )
            assert total == 2, f"page {page} reported a count the filters should have reduced"
            assert len(uuids) == 1
            collected.extend(uuids)

        assert set(collected) == {str(kept_one.uuid), str(kept_two.uuid)}
        assert len(collected) == len(set(collected)), "a hit was served on two different pages"
        assert str(excluded.uuid) not in collected

        # The page AFTER the last one the count promises is empty, and the count
        # does not move.
        total, uuids = _summary_search(
            client, user_token_headers, "roadmap", tags=["quarterly"], page=3, page_size=1
        )
        assert uuids == []
        assert total == 2

    def test_total_pages_is_derived_from_the_filtered_count(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        """``total_pages`` is what the SPA renders the pager from, so an
        unfiltered count reaches the user as pages that do not exist.
        """
        kept = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        _tag_file(db_session, normal_user, kept, "quarterly")

        response = client.get(
            SEARCH_PATH,
            params={
                "q": "roadmap",
                "result_type": "summaries",
                "tags": ["quarterly"],
                "page_size": 1,
            },
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary_total"] == 1
        assert body["total_pages"] == 1


# --------------------------------------------------------------------------- #
# result_type=all pagination (issue #831 item 3)                                #
#                                                                               #
# `total_pages` is the only paging signal in the response, so for `all` it must  #
# reach the end of BOTH legs. It used to be the transcript leg's value alone.    #
#                                                                               #
# Reachability note: the SPA cannot send `result_type=all` today — its           #
# `SearchResultType` is `'transcripts' | 'summaries'` (frontend/src/stores/      #
# search.ts) and the tab strip offers exactly those two. This is the server-side #
# math, fixed ahead of #760 making `all` reachable.                              #
# --------------------------------------------------------------------------- #


def _walk_all_pages(client, headers, q: str, page_size: int):
    """Walk every page ``total_pages`` promises; return the two legs' collected ids.

    Returns ``(transcript_uuids, summary_uuids, summary_total, total_pages)``,
    each id list in the order the pages handed them out — so a caller can assert
    on duplicates, not merely on the set.
    """
    first = client.get(
        SEARCH_PATH,
        params={"q": q, "result_type": "all", "page": 1, "page_size": page_size},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    total_pages = body["total_pages"]
    summary_total = body["summary_total"]

    transcript_uuids = [hit["file_uuid"] for hit in body["results"]]
    summary_uuids = [hit["file_uuid"] for hit in body["summary_results"]]

    for page in range(2, total_pages + 1):
        response = client.get(
            SEARCH_PATH,
            params={"q": q, "result_type": "all", "page": page, "page_size": page_size},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        page_body = response.json()
        assert page_body["summary_total"] == summary_total, (
            f"page {page} reported a different summary_total than page 1"
        )
        transcript_uuids.extend(hit["file_uuid"] for hit in page_body["results"])
        summary_uuids.extend(hit["file_uuid"] for hit in page_body["summary_results"])

    return transcript_uuids, summary_uuids, summary_total, total_pages


class TestResultTypeAllPagesBothLegs:
    def test_walking_every_page_yields_each_summary_exactly_once(
        self, client, user_token_headers, normal_user, db_session, paginating_transcript_leg
    ):
        """THE item-3 test. The summary leg is given MORE pages than the
        transcript leg, which is the direction that lost results: `total_pages`
        came from the transcript leg, so the client stopped walking before the
        summary leg was exhausted and the remaining summaries were never
        requested at all.
        """
        paginating_transcript_leg(1)  # one transcript hit -> one transcript page
        expected = {
            str(_make_file(db_session, normal_user, summary={"bluf": "roadmap review"}).uuid)
            for _ in range(5)
        }

        _, summary_uuids, summary_total, total_pages = _walk_all_pages(
            client, user_token_headers, "roadmap", page_size=2
        )

        assert summary_uuids, "walked the pages and collected nothing — vacuous"
        assert summary_total == 5
        assert total_pages == 3, "the pager must reach the summary leg's last page"
        assert len(summary_uuids) == len(set(summary_uuids)), (
            "a summary was served on two different pages"
        )
        assert set(summary_uuids) == expected
        assert len(summary_uuids) == summary_total

    def test_walking_every_page_yields_each_transcript_hit_exactly_once(
        self, client, user_token_headers, normal_user, db_session, paginating_transcript_leg
    ):
        """The mirror, as the control: with the TRANSCRIPT leg longer the page
        count was already right, so this direction must keep working — otherwise
        a fix that simply swapped which leg wins would look like a pass.
        """
        expected = set(paginating_transcript_leg(5))
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})

        transcript_uuids, summary_uuids, summary_total, total_pages = _walk_all_pages(
            client, user_token_headers, "roadmap", page_size=2
        )

        assert transcript_uuids, "walked the pages and collected nothing — vacuous"
        assert total_pages == 3
        assert len(transcript_uuids) == len(set(transcript_uuids)), (
            "a transcript hit was served on two different pages"
        )
        assert set(transcript_uuids) == expected
        # The shorter leg runs out and returns nothing more, rather than
        # repeating its last page.
        assert summary_total == 1
        assert len(summary_uuids) == 1

    def test_a_page_past_the_end_of_one_leg_still_serves_the_other(
        self, client, user_token_headers, normal_user, db_session, paginating_transcript_leg
    ):
        """The per-page shape behind the walk above: on page 3 the transcript leg
        is long exhausted, and the summary leg must still answer rather than the
        page collapsing to empty.
        """
        paginating_transcript_leg(1)
        for _ in range(5):
            _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})

        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "all", "page": 3, "page_size": 2},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["results"] == []
        assert len(body["summary_results"]) == 1
        assert body["summary_total"] == 5
