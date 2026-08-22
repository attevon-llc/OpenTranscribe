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

import time
import uuid as uuid_pkg
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import TransportError

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
# 8. Live equivalence check, ready for when someone proposes the flip.
# --------------------------------------------------------------------------- #

_OPENSEARCH_ABSENT = __import__("os").environ.get("SKIP_OPENSEARCH", "True").lower() == "true"


@pytest.mark.skipif(
    _OPENSEARCH_ABSENT,
    reason="No OpenSearch reachable (SKIP_OPENSEARCH) — this check reads the live chunk plane.",
)
def test_speaker_id_and_speaker_name_filters_agree_on_the_live_index():
    """Not part of the flip decision itself — the instrument it would need.

    Restricted to documents that already carry `speaker_id` (coverage is expected
    near 0% before backfill runs, per the gate report), so it is meaningful at ANY
    coverage level rather than only after a full backfill: for the subset that has
    both a name and an id, do the two filters select the same chunks?

    SKIPS (not fails) when nothing has been backfilled yet — that is the honestly
    expected state today, and asserting equivalence over an empty sample would be
    the vacuous-loop shape `scripts/audit-tests.py` flags, not a real check.
    """
    from app.services.opensearch_service import get_opensearch_client

    maybe_client = get_opensearch_client()
    if not maybe_client or not maybe_client.indices.exists(index=_INDEX):
        pytest.skip("chunks index not present on this cluster")
    # `pytest.skip` is NoReturn, but the pre-commit mypy hook runs in an isolated
    # env with no pytest stubs, so it cannot narrow through the guard above — and a
    # narrowed name would not stay narrowed inside `_ids`' closure below anyway.
    # The assert states the invariant the guard already established, and binds it to
    # a name that is never reassigned.
    assert maybe_client is not None
    client = maybe_client

    chunk_clause = digest_mapping.chunk_plane_clause()

    def _search(body: dict[str, Any]) -> dict[str, Any]:
        """Search the LIVE index, tolerating a rebuild in progress.

        This test reads the real dev index, which other processes rewrite: every
        ``backend/app/**`` save hot-reloads the backend, and startup dispatches
        ``search_index_maintenance``, which reindexes. A search landing in that
        window returns ``503 search_phase_execution_exception`` / "all shards
        failed" with an EMPTY ``root_cause`` and EMPTY ``failed_shards`` — the
        signature of a shard being momentarily unavailable rather than of a bad
        query. Measured: 3 consecutive failures during an edit burst, then 30
        consecutive successes once edits stopped, with the cluster reporting
        green throughout.

        So retry a bounded number of times — and then **FAIL**, never skip. A
        persistent 503 is a real defect (a malformed body, or a field the mapping
        does not carry) and must not be laundered into a green run by a broad
        except. The retry only absorbs the transient rebuild window.
        """
        last: Exception | None = None
        for attempt in range(4):
            try:
                return cast(dict[str, Any], client.search(index=_INDEX, body=body))
            except TransportError as exc:  # noqa: PERF203 - retry is the point
                if getattr(exc, "status_code", None) != 503:
                    raise
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise AssertionError(
            f"the chunk index stayed unavailable across 4 attempts ({last}). "
            "That is no longer a rebuild window — investigate the query or the mapping."
        )

    sample = _search(
        {
            "size": 1,
            "query": {"bool": {"filter": [chunk_clause, {"exists": {"field": "speaker_id"}}]}},
            "_source": ["speaker_id", "speaker", "file_uuid"],
        }
    )
    hits = sample["hits"]["hits"]
    if not hits:
        pytest.skip(
            "No chunk carries speaker_id yet — coverage is 0% before backfill runs "
            "(issue #W2.7c). This test becomes meaningful once some coverage exists."
        )

    source = hits[0]["_source"]
    speaker_id, speaker_name = source["speaker_id"], source["speaker"]
    file_uuid = source["file_uuid"]

    result_cap = 2000

    def _ids(extra_clause: dict[str, Any]) -> set[str]:
        resp = _search(
            {
                "size": result_cap,
                "query": {"bool": {"filter": [chunk_clause, extra_clause]}},
                "_source": False,
            }
        )
        got = {hit["_id"] for hit in resp["hits"]["hits"]}
        # A set truncated at the cap cannot be compared to a complete one — the
        # original version of this test compared 72 ids against a set pinned at
        # its own `size: 500`, which is not an equivalence, it is an artefact.
        assert len(got) < result_cap, (
            f"result hit the {result_cap} cap, so this set is truncated and any set "
            "comparison below would be meaningless. Raise the cap or narrow the query."
        )
        return got

    # ⚠️ THE INVARIANT IS ONE-WAY, and that is a property of the data model.
    #
    # A `speaker_id` is a `Speaker` ROW, and diarization legitimately produces several
    # rows for the same person in one recording — measured on the dev index, inside a
    # single file "Joe Rogan" is speaker_id 2811 (347 chunks) AND 2812 (72 chunks),
    # because two diarized clusters were both identified as him. Across files it is
    # worse: 16 distinct speaker_ids over 12 recordings.
    #
    # So `terms(speaker_id)` is SOUND but not COMPLETE with respect to `terms(speaker)`:
    # it never selects a chunk belonging to someone else, but it does not select every
    # chunk belonging to this person. An earlier version of this test asserted set
    # EQUALITY in both directions and was therefore asserting something the architecture
    # never claimed — first globally, then per-file. Both were wrong.
    #
    # This is exactly why W2.7d specifies the flip as a UNION,
    # `should: [terms(speaker_id), terms(speaker)]`, rather than a substitution — and
    # why the cross-recording identity is `profile_id`, not `speaker_id`.
    same_file = {"term": {"file_uuid": file_uuid}}
    by_id = _ids({"bool": {"filter": [{"term": {"speaker_id": speaker_id}}, same_file]}})
    by_name_same_file = _ids(
        {
            "bool": {
                "filter": [
                    {"term": {"speaker": speaker_name}},
                    {"exists": {"field": "speaker_id"}},
                    same_file,
                ]
            }
        }
    )

    assert by_id, "the sampled speaker_id itself did not round-trip through its own filter"

    # SOUNDNESS: every chunk the id selects is also selected by that speaker's name.
    # This is the property a filter flip actually depends on — it is what guarantees an
    # id-keyed filter can never surface another person's words.
    assert by_id <= by_name_same_file, (
        f"{len(by_id - by_name_same_file)} chunk(s) matched speaker_id={speaker_id} but "
        f"NOT the name {speaker_name!r} in the same file. The id and the label are out of "
        "step, which means the write path attached an id to the wrong speaker's chunk."
    )

    # And the direct form of the same guarantee, read from the documents themselves:
    # a speaker_id addresses exactly ONE display name.
    resp = _search(
        {
            "size": 0,
            "query": {"bool": {"filter": [chunk_clause, {"term": {"speaker_id": speaker_id}}]}},
            "aggs": {"names": {"terms": {"field": "speaker", "size": 10}}},
        }
    )
    names = [b["key"] for b in resp["aggregations"]["names"]["buckets"]]
    assert names == [speaker_name], (
        f"speaker_id={speaker_id} resolves to {names} — a single Speaker row must carry "
        "exactly one display name, so more than one means two speakers share an id."
    )
