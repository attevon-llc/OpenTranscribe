"""``speaker_id`` / ``profile_id`` — the first two fields shipped through the additive
mapping machinery (issue #W2.7b/c/d).

Three properties matter more than the fields themselves:

1. **They are integers with no ``eager_global_ordinals``** — unlike ``speaker``/``tags``,
   which are keyword-faceted today. These exist to be filtered on later, not aggregated
   now, and eagerly building ordinals for a mostly-sparse field before anything reads it
   would be pure cost.
2. **They must NEVER reach ``embedding_text``.** That field is what the neural ingest
   pipeline embeds; an id folded into it would reshape the vector of every document that
   carries one, which is exactly the re-embed this whole mechanism exists to avoid paying.
3. **Every reader needs an ``exists`` compat arm.** Old documents (everything until they
   are next reindexed) carry neither field at all, the same shape the existing
   ``doc_type``/``chunk_plane_clause`` compat arm handles — see
   ``tests/unit/test_digest_index_mapping.py::test_a_bare_term_would_exclude_every_document_already_indexed``
   for the sibling case this mirrors.

The read path (``chunk_retrieval.py``, the search filter) is deliberately UNCHANGED —
flipping speaker filters from names to ids is a later, measured decision (services/search/
CLAUDE.md). What ships now is the coverage instrument that decision would read, and an
id-vs-name equivalence check ready for when someone proposes it.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

import app.services.search.indexing_service as svc
from app.core.config import settings
from app.services.ingest_artifacts import index_mapping as digest_mapping
from app.services.search.chunking_service import chunk_transcript_by_speaker_turns
from app.tasks import reindex_task

_INDEX = settings.OPENSEARCH_CHUNKS_INDEX

BASE_KWARGS: dict[str, Any] = {
    "file_uuid": "11111111-2222-3333-4444-555555555555",
    "file_id": 42,
    "user_id": 7,
    "title": "Speaker Id Fixture",
    "speakers": ["Dana", "Marcus"],
    "tags": [],
    "upload_time": "2026-01-01T00:00:00Z",
}


def _segments(*, speaker_id: int | None, profile_id: int | None) -> list[dict[str, Any]]:
    return [
        {
            "start": 0.0,
            "end": 5.0,
            "text": "We agreed to ship the release on Friday.",
            "speaker": "Dana",
            "speaker_id": speaker_id,
            "profile_id": profile_id,
        }
    ]


# --------------------------------------------------------------------------- #
# 1. Mapping shape.
# --------------------------------------------------------------------------- #


def test_the_step_defines_plain_integers_with_no_eager_global_ordinals():
    step = next(s for s in svc.ADDITIVE_MAPPING_STEPS if "speaker_id" in s.properties)
    assert step.properties["speaker_id"] == {"type": "integer"}
    assert step.properties["profile_id"] == {"type": "integer"}
    assert "eager_global_ordinals" not in step.properties["speaker_id"]
    assert "eager_global_ordinals" not in step.properties["profile_id"]


def test_eager_global_ordinals_stays_on_the_keyword_facets_only():
    """Control: the existing keyword fields still opt in — this isn't a blanket removal."""
    mappings = cast(dict[str, Any], svc.TRANSCRIPT_CHUNKS_INDEX_BODY["mappings"])
    properties = mappings["properties"]
    assert properties["speaker"].get("eager_global_ordinals") is True
    assert properties["tags"].get("eager_global_ordinals") is True


# --------------------------------------------------------------------------- #
# 2. Chunking write path.
# --------------------------------------------------------------------------- #


def test_speaker_id_and_profile_id_are_written_when_known():
    chunks = chunk_transcript_by_speaker_turns(
        _segments(speaker_id=123, profile_id=456), **BASE_KWARGS
    )
    assert len(chunks) == 1
    assert chunks[0]["speaker_id"] == 123
    assert chunks[0]["profile_id"] == 456


def test_speaker_id_and_profile_id_are_absent_when_unknown():
    """The compat-arm precondition: a chunk from an unresolved segment carries NEITHER
    key at all, not an explicit ``null`` — matching ``organization_id``'s existing
    only-when-known convention (``test_chunking_service.py``).
    """
    chunks = chunk_transcript_by_speaker_turns(
        _segments(speaker_id=None, profile_id=None), **BASE_KWARGS
    )
    assert len(chunks) == 1
    assert "speaker_id" not in chunks[0]
    assert "profile_id" not in chunks[0]


def test_a_known_speaker_id_with_no_resolved_profile_omits_profile_id_only():
    chunks = chunk_transcript_by_speaker_turns(
        _segments(speaker_id=123, profile_id=None), **BASE_KWARGS
    )
    assert chunks[0]["speaker_id"] == 123
    assert "profile_id" not in chunks[0]


# --------------------------------------------------------------------------- #
# 3. The ids must never reach embedding_text (or content).
# --------------------------------------------------------------------------- #


def test_ids_do_not_appear_in_chunk_content():
    chunks = chunk_transcript_by_speaker_turns(
        _segments(speaker_id=999999, profile_id=888888), **BASE_KWARGS
    )
    assert "999999" not in chunks[0]["content"]
    assert "888888" not in chunks[0]["content"]


def test_embedding_text_is_identical_regardless_of_speaker_id_or_profile_id():
    """Reproduces the exact formula ``index_transcript_chunks`` applies after
    chunking (``indexing_service.py``'s ``chunk["embedding_text"] = build_embedding_text(
    title=..., recorded_at=..., roster=..., body=str(chunk.get("content") or ""))``),
    with two runs differing ONLY in speaker_id/profile_id. If an id ever leaked into
    the body or the header, this would be the test that catches it.
    """
    with_ids = chunk_transcript_by_speaker_turns(
        _segments(speaker_id=123, profile_id=456), **BASE_KWARGS
    )
    without_ids = chunk_transcript_by_speaker_turns(
        _segments(speaker_id=None, profile_id=None), **BASE_KWARGS
    )
    assert with_ids[0]["content"] == without_ids[0]["content"]

    roster = sorted(BASE_KWARGS["speakers"])
    text_with_ids = digest_mapping.build_embedding_text(
        title=BASE_KWARGS["title"],
        recorded_at=BASE_KWARGS["upload_time"],
        roster=roster,
        body=with_ids[0]["content"],
    )
    text_without_ids = digest_mapping.build_embedding_text(
        title=BASE_KWARGS["title"],
        recorded_at=BASE_KWARGS["upload_time"],
        roster=roster,
        body=without_ids[0]["content"],
    )
    assert text_with_ids == text_without_ids


def test_build_embedding_text_has_no_speaker_or_profile_id_parameter():
    """Structural backstop: the function the pipeline embeds through cannot accept an
    id even if a future caller tried to pass one.
    """
    import inspect

    params = set(inspect.signature(digest_mapping.build_embedding_text).parameters)
    assert "speaker_id" not in params
    assert "profile_id" not in params


# --------------------------------------------------------------------------- #
# 4. reindex_task write path (real Postgres — the batch reindex source of truth).
# --------------------------------------------------------------------------- #


def _make_user(db_session):
    from app.core.security import get_password_hash
    from app.models.user import User

    uid = uuid_pkg.uuid4().hex[:10]
    user = User(
        email=f"speaker-id-fixture-{uid}@example.com",
        full_name="Speaker Id Fixture",
        hashed_password=get_password_hash("password123"),  # noqa: S106 — throwaway fixture row
        is_active=True,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_reindex_extract_file_metadata_carries_speaker_id_and_profile_id(db_session):
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile
    from app.models.media import TranscriptSegment

    user = _make_user(db_session)
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"speaker-id-{fuuid.hex[:8]}.wav",
        storage_path=f"media/speaker-id/{fuuid}.wav",
        content_type="audio/wav",
        file_size=100,
        user_id=user.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    profile = SpeakerProfile(user_id=user.id, name="Dana Profile")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    speaker = Speaker(
        user_id=user.id,
        media_file_id=media_file.id,
        profile_id=profile.id,
        name="SPEAKER_00",
        display_name="Dana",
    )
    unresolved_speaker = Speaker(user_id=user.id, media_file_id=media_file.id, name="SPEAKER_01")
    db_session.add_all([speaker, unresolved_speaker])
    db_session.commit()
    db_session.refresh(speaker)
    db_session.refresh(unresolved_speaker)

    db_session.add_all(
        [
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=0.0,
                end_time=2.0,
                text="Resolved-speaker segment.",
            ),
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                speaker_id=unresolved_speaker.id,
                start_time=2.0,
                end_time=4.0,
                text="Speaker with no profile.",
            ),
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                speaker_id=None,
                start_time=4.0,
                end_time=6.0,
                text="No speaker at all.",
            ),
        ]
    )
    db_session.commit()

    metadata = reindex_task._extract_file_metadata(db_session, media_file)
    assert metadata is not None
    by_text = {seg["text"]: seg for seg in metadata["segments"]}

    resolved = by_text["Resolved-speaker segment."]
    assert resolved["speaker_id"] == speaker.id
    assert resolved["profile_id"] == profile.id

    no_profile = by_text["Speaker with no profile."]
    assert no_profile["speaker_id"] == unresolved_speaker.id
    assert "profile_id" not in no_profile, (
        "a Speaker row with profile_id=NULL must not write an explicit null — "
        "readers use `exists`, not a null check"
    )

    no_speaker = by_text["No speaker at all."]
    assert "speaker_id" not in no_speaker
    assert "profile_id" not in no_speaker


# --------------------------------------------------------------------------- #
# 5. The coverage instrument the future flip-gate would read.
# --------------------------------------------------------------------------- #


def _mock_client(*, exists: bool = True) -> MagicMock:
    client = MagicMock()
    client.indices.exists.return_value = exists
    return client


def test_survey_speaker_id_coverage_is_unavailable_with_no_client():
    with patch.object(svc, "opensearch_client", None):
        result = svc.survey_speaker_id_coverage()
    assert result["verdict"] == "unavailable"


def test_survey_speaker_id_coverage_is_unavailable_when_the_index_is_absent():
    client = _mock_client(exists=False)
    with patch.object(svc, "opensearch_client", client):
        result = svc.survey_speaker_id_coverage()
    assert result["verdict"] == "unavailable"
    client.search.assert_not_called()


def test_survey_speaker_id_coverage_reports_empty_for_zero_chunk_plane_documents():
    client = _mock_client()
    client.search.return_value = {"hits": {"total": {"value": 0}}, "aggregations": {}}
    with patch.object(svc, "opensearch_client", client):
        result = svc.survey_speaker_id_coverage()
    assert result == {"verdict": "empty", "total": 0, "with_speaker_id": 0, "coverage_ratio": 0.0}


def test_survey_speaker_id_coverage_computes_the_ratio():
    client = _mock_client()
    client.search.return_value = {
        "hits": {"total": {"value": 200}},
        "aggregations": {"with_speaker_id": {"doc_count": 50}},
    }
    with patch.object(svc, "opensearch_client", client):
        result = svc.survey_speaker_id_coverage()
    assert result == {
        "verdict": "measured",
        "total": 200,
        "with_speaker_id": 50,
        "coverage_ratio": 0.25,
    }


def test_survey_speaker_id_coverage_uses_a_filter_agg_never_a_terms_agg_on_file_uuid():
    """Guards against inheriting `_get_indexed_uuids`'s 50,000-bucket ceiling.

    A `filter` aggregation returns one bounded doc_count at any corpus size; a
    `terms` aggregation on `file_uuid` (the shape that ceiling belongs to) would
    silently undercount past 50k distinct files. This function must use the former.
    """
    client = _mock_client()
    client.search.return_value = {
        "hits": {"total": {"value": 1}},
        "aggregations": {"with_speaker_id": {"doc_count": 1}},
    }
    with patch.object(svc, "opensearch_client", client):
        svc.survey_speaker_id_coverage()

    body = client.search.call_args.kwargs["body"]
    aggs = body["aggs"]["with_speaker_id"]
    assert "filter" in aggs
    assert aggs["filter"] == {"exists": {"field": "speaker_id"}}
    assert "terms" not in str(aggs)


# --------------------------------------------------------------------------- #
# 6. The opt-in backfill task — a thin wrapper, never a second write path.
# --------------------------------------------------------------------------- #


def _composite_response(buckets: list[dict[str, Any]], after_key: dict[str, Any] | None) -> dict:
    files_agg: dict[str, Any] = {"buckets": buckets}
    if after_key is not None:
        files_agg["after_key"] = after_key
    return {"aggregations": {"files": files_agg}}


def test_backfill_task_is_not_wired_to_run_automatically():
    """It must be a plain, undecorated-by-schedule Celery task — nothing beats it in.

    A grep-shaped guard: the task name must not appear in `core/celery.py`'s
    beat schedule (`CELERYBEAT_SCHEDULE` / `beat_schedule`). Read the file directly
    rather than importing celery_app's live schedule, so the check is independent of
    whatever else happens to be configured at import time in this test process.

    The lookup asserts the block was actually found (this repo's `celery.py` HAS a
    `beat_schedule=`, so "not found" would mean the parser broke, not "nothing is
    scheduled") before checking membership — so both halves run unconditionally
    rather than the membership check being the only assertion, hidden behind an
    `if "beat_schedule" in source` that could quietly stop firing if the file were
    ever reformatted with no schedule at all.
    """
    import pathlib

    celery_config = pathlib.Path(reindex_task.__file__).parent.parent / "core" / "celery.py"
    source = celery_config.read_text()
    # Find the beat-schedule block and confirm our task name never appears inside
    # it. A bare substring check against the WHOLE file would also match this
    # task's own `@celery_app.task(name=...)` declaration in search_indexing_task.py,
    # which is not what we're checking.
    start = source.find("beat_schedule")
    assert start != -1, "core/celery.py has no beat_schedule= at all — parser assumption broke"
    end = source.find("\n}\n", start)
    block = source[start : end if end != -1 else None]
    assert "backfill_speaker_id_fields" not in block


def test_backfill_task_dispatches_one_reindex_per_user_for_missing_files():
    from app.tasks.search_indexing_task import backfill_speaker_id_fields_task

    client = _mock_client()
    client.search.return_value = _composite_response(
        [
            {"key": {"user_id": 1, "file_uuid": "aaaa"}, "doc_count": 3},
            {"key": {"user_id": 1, "file_uuid": "bbbb"}, "doc_count": 2},
            {"key": {"user_id": 2, "file_uuid": "cccc"}, "doc_count": 1},
        ],
        after_key=None,
    )

    with (
        patch(
            "app.services.opensearch_service.get_opensearch_client",
            return_value=client,
        ),
        patch("app.tasks.reindex_task.reindex_transcripts_task") as mocked_reindex,
    ):
        result = backfill_speaker_id_fields_task(limit=50)

    assert result["status"] == "dispatched"
    assert result["dispatched_files"] == 3
    assert result["dispatched_users"] == 2
    assert mocked_reindex.apply_async.call_count == 2

    dispatched = {
        call.kwargs["args"][0]: set(call.kwargs["args"][1])
        for call in mocked_reindex.apply_async.call_args_list
    }
    assert dispatched[1] == {"aaaa", "bbbb"}
    assert dispatched[2] == {"cccc"}


def test_backfill_task_skips_when_fully_covered():
    from app.tasks.search_indexing_task import backfill_speaker_id_fields_task

    client = _mock_client()
    client.search.return_value = _composite_response([], after_key=None)

    with patch("app.services.opensearch_service.get_opensearch_client", return_value=client):
        result = backfill_speaker_id_fields_task(limit=50)

    assert result == {"status": "skipped", "reason": "fully_covered"}


def test_backfill_task_paginates_via_after_key_not_a_single_bare_page():
    from app.tasks.search_indexing_task import backfill_speaker_id_fields_task

    client = _mock_client()
    page_one = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "aaaa"}, "doc_count": 1}],
        after_key={"user_id": 1, "file_uuid": "aaaa"},
    )
    page_two = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "bbbb"}, "doc_count": 1}],
        after_key=None,
    )
    client.search.side_effect = [page_one, page_two]

    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch("app.tasks.reindex_task.reindex_transcripts_task") as mocked_reindex,
    ):
        result = backfill_speaker_id_fields_task(limit=50)

    assert client.search.call_count == 2
    second_call_composite = client.search.call_args_list[1].kwargs["body"]["aggs"]["files"][
        "composite"
    ]
    assert second_call_composite["after"] == {"user_id": 1, "file_uuid": "aaaa"}
    assert result["dispatched_files"] == 2
    assert mocked_reindex.apply_async.call_count == 1  # both files belong to user 1


def test_backfill_task_never_calls_a_raw_opensearch_write():
    """It is a THIN wrapper around the reindex coordinator, never a second bulk
    write path — this is what makes it safe to run against a live corpus at all.
    """
    from app.tasks.search_indexing_task import backfill_speaker_id_fields_task

    client = _mock_client()
    client.search.return_value = _composite_response(
        [{"key": {"user_id": 1, "file_uuid": "aaaa"}, "doc_count": 1}], after_key=None
    )

    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch("app.tasks.reindex_task.reindex_transcripts_task"),
    ):
        result = backfill_speaker_id_fields_task(limit=50)

    # Real state: the task still did its job (dispatched a reindex) — the absence
    # checks below are not standing in for a task that silently did nothing.
    assert result["status"] == "dispatched"
    client.bulk.assert_not_called()
    client.update_by_query.assert_not_called()
    client.indices.put_mapping.assert_not_called()


# --------------------------------------------------------------------------- #
# 7. Compat arm: `exists` must be checked, never assumed.
# --------------------------------------------------------------------------- #


def test_a_legacy_chunk_fails_an_exists_check_on_speaker_id():
    """The precondition every future reader of this field must respect: a document
    written before these fields existed satisfies no ``exists`` query on either one.
    """
    legacy_chunk = {"file_uuid": "u", "chunk_index": 0, "content": "...", "speaker": "Dana"}
    assert "speaker_id" not in legacy_chunk
    assert "profile_id" not in legacy_chunk

    new_chunk = dict(legacy_chunk, speaker_id=1, profile_id=2)
    assert "speaker_id" in new_chunk
    assert "profile_id" in new_chunk


# --------------------------------------------------------------------------- #
# 8. The id/name equivalence instrument, against data this file OWNS.
# --------------------------------------------------------------------------- #
#
# This section used to sample the LIVE dev chunk index. It was wrong twice over:
#
#   * It could not fail honestly. `speaker_id` coverage is ~0% before backfill (issue #W2.7c),
#     so the sample came back empty and it SKIPPED — a green run proving nothing, on every
#     machine, most of the time.
#   * When it did run it raced the reindexer. Its own docstring documented the window: every
#     `backend/app/**` save hot-reloads the backend, startup dispatches
#     `search_index_maintenance`, and a search landing mid-rebuild returns a 503 with empty
#     `root_cause`. On 2026-09-07 that 503 persisted past all 4 retries and failed the gate's
#     Unit/API phase — the single real failure in a 14,380-test run, and nothing to do with the
#     code under test.
#
# The invariants are worth keeping, so they are now asserted against a throwaway index this
# file creates and deletes, seeded to include the awkward real-world shape the comments below
# describe (one person, two speaker_ids, same file). Deterministic at any backfill coverage,
# and a negative control proves the check can still fail.

_OPENSEARCH_ABSENT = __import__("os").environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

_needs_opensearch = pytest.mark.skipif(
    _OPENSEARCH_ABSENT,
    reason="No OpenSearch reachable (SKIP_OPENSEARCH) — these drive a real cluster.",
)


def _seed_chunk(client, index: str, *, doc_id: str, file_uuid: str, speaker: str, speaker_id: int):
    client.index(
        index=index,
        id=doc_id,
        body={
            "file_uuid": file_uuid,
            "chunk_index": 0,
            "content": "seeded chunk for the speaker-id/name equivalence invariant",
            "title": "speaker-id fixture",
            "speaker": speaker,
            "speaker_id": speaker_id,
            "speakers": [speaker],
            "tags": [],
            "content_type": "audio/wav",
            "accessible_user_ids": [1],
            "upload_time": "2026-09-07T00:00:00+00:00",
            "language": "en",
            "start_time": 0.0,
            "end_time": 9.0,
            "indexed_at": "2026-09-07T00:00:00+00:00",
            "doc_type": "chunk",
        },
    )


@pytest.fixture
def seeded_chunk_index():
    """A throwaway chunks index with the REAL mapping, deleted in teardown.

    The real mapping matters: `speaker` must be a `keyword` for the exact `term` filters and
    the terms aggregation below to mean anything.
    """
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    # An assert, not a skip — the module gate above already established a reachable cluster,
    # so a None client means the factory is broken and must not be laundered into a green run.
    assert client is not None, (
        "SKIP_OPENSEARCH reported a reachable cluster but get_opensearch_client() returned None"
    )

    name = f"test_speaker_id_fields_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    try:
        yield client, name
    finally:
        client.indices.delete(index=name, ignore=[404])


@_needs_opensearch
def test_a_speaker_id_filter_never_selects_another_persons_chunks(seeded_chunk_index):
    """SOUNDNESS — the property a filter flip actually depends on.

    ⚠️ THE INVARIANT IS ONE-WAY, and that is a property of the data model. A `speaker_id` is a
    `Speaker` ROW, and diarization legitimately produces several rows for the same person in one
    recording — measured on the dev index, "Joe Rogan" was speaker_id 2811 (347 chunks) AND 2812
    (72 chunks). So `terms(speaker_id)` is SOUND but not COMPLETE with respect to
    `terms(speaker)`: it never selects someone else's chunks, but it does not select all of that
    person's. An earlier version asserted set EQUALITY and was asserting something the
    architecture never claimed.

    The seed reproduces exactly that shape, so the one-way-ness is exercised rather than assumed.
    """
    client, index = seeded_chunk_index
    file_uuid = str(uuid_pkg.uuid4())

    # One person, TWO speaker_ids, same file — the documented real case.
    _seed_chunk(client, index, doc_id="a1", file_uuid=file_uuid, speaker="Dana", speaker_id=1)
    _seed_chunk(client, index, doc_id="a2", file_uuid=file_uuid, speaker="Dana", speaker_id=1)
    _seed_chunk(client, index, doc_id="b1", file_uuid=file_uuid, speaker="Dana", speaker_id=2)
    # A different person in the same file, who must never be selected by Dana's ids.
    _seed_chunk(client, index, doc_id="c1", file_uuid=file_uuid, speaker="Evan", speaker_id=3)
    client.indices.refresh(index=index)

    chunk_clause = digest_mapping.chunk_plane_clause()
    same_file = {"term": {"file_uuid": file_uuid}}

    def _ids(extra_clause: dict[str, Any]) -> set[str]:
        resp = cast(
            dict[str, Any],
            client.search(
                index=index,
                body={
                    "size": 100,
                    "query": {"bool": {"filter": [chunk_clause, extra_clause]}},
                    "_source": False,
                },
            ),
        )
        return {hit["_id"] for hit in resp["hits"]["hits"]}

    by_id = _ids({"bool": {"filter": [{"term": {"speaker_id": 1}}, same_file]}})
    by_name = _ids(
        {
            "bool": {
                "filter": [
                    {"term": {"speaker": "Dana"}},
                    {"exists": {"field": "speaker_id"}},
                    same_file,
                ]
            }
        }
    )

    assert by_id == {"a1", "a2"}, (
        f"the seeded id did not round-trip through its own filter: {by_id}"
    )
    assert by_id <= by_name, (
        f"{len(by_id - by_name)} chunk(s) matched speaker_id=1 but NOT the name 'Dana' in the "
        "same file. The id and the label are out of step, which means the write path attached "
        "an id to the wrong speaker's chunk."
    )
    # And the one-way-ness itself, asserted rather than described: the name selects MORE.
    assert by_id < by_name, (
        "the seed deliberately gives Dana a second speaker_id, so the name must select a "
        "strict superset — if these are equal the fixture stopped exercising the real shape "
        "and the soundness assertion above became trivially true"
    )
    assert "c1" not in by_name, "another speaker's chunk was selected by Dana's name filter"


@_needs_opensearch
def test_one_speaker_id_resolves_to_exactly_one_display_name(seeded_chunk_index):
    """A single `Speaker` row must carry exactly one display name."""
    client, index = seeded_chunk_index
    file_uuid = str(uuid_pkg.uuid4())

    _seed_chunk(client, index, doc_id="a1", file_uuid=file_uuid, speaker="Dana", speaker_id=1)
    _seed_chunk(client, index, doc_id="a2", file_uuid=file_uuid, speaker="Dana", speaker_id=1)
    client.indices.refresh(index=index)

    assert _names_for_speaker_id(client, index, 1) == ["Dana"]


@_needs_opensearch
def test_two_names_under_one_speaker_id_is_detected(seeded_chunk_index):
    """NEGATIVE CONTROL — the defect the test above exists to catch, seeded on purpose.

    Without this, `test_one_speaker_id_resolves_to_exactly_one_display_name` would pass just as
    happily against an aggregation that could never return two buckets.
    """
    client, index = seeded_chunk_index
    file_uuid = str(uuid_pkg.uuid4())

    _seed_chunk(client, index, doc_id="a1", file_uuid=file_uuid, speaker="Dana", speaker_id=1)
    _seed_chunk(client, index, doc_id="a2", file_uuid=file_uuid, speaker="Evan", speaker_id=1)
    client.indices.refresh(index=index)

    assert sorted(_names_for_speaker_id(client, index, 1)) == ["Dana", "Evan"], (
        "two display names under one speaker_id must be visible to this query — if it reports "
        "one, the check above cannot fail and proves nothing"
    )


def _names_for_speaker_id(client, index: str, speaker_id: int) -> list[str]:
    resp = cast(
        dict[str, Any],
        client.search(
            index=index,
            body={
                "size": 0,
                "query": {
                    "bool": {
                        "filter": [
                            digest_mapping.chunk_plane_clause(),
                            {"term": {"speaker_id": speaker_id}},
                        ]
                    }
                },
                "aggs": {"names": {"terms": {"field": "speaker", "size": 10}}},
            },
        ),
    )
    return [b["key"] for b in resp["aggregations"]["names"]["buckets"]]
