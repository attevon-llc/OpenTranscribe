"""``opensearch_service.speaker_metadata`` — real reads/updates + one real sync.

Real writes/reads against throwaway speaker + transcript indices on the live
dev OpenSearch cluster, and (for ``sync_speaker_profiles_to_opensearch``) a
real Postgres ``Speaker``/``SpeakerProfile`` row via the savepoint-rolled-back
``db_session`` fixture.

No kNN ``_score`` is ever read in this module — ``find_speaker_across_media``
does a plain ``get`` by id followed by a ``term`` search, never a kNN query —
so the cosinesimil conversion rule does not apply here.
"""

from __future__ import annotations

import datetime
import os
import uuid as uuid_pkg
from types import SimpleNamespace

import pytest
from opensearchpy.exceptions import NotFoundError

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services.opensearch_service import speaker_metadata

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"


# Serialised against the other live-cluster speaker suites (issue #486). All three create and
# delete throwaway OpenSearch indices; under the default `-n auto` (24 workers here) that
# index churn overloads the single dev cluster and reads start timing out at 10 s, which
# surfaces as an unrelated-looking assertion failure in whichever test lost the race.
# Measured: 1 failure in 4 concurrent runs, on a different test each time.
pytestmark = [
    pytest.mark.xdist_group("opensearch_speaker_indices"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). These tests verify real "
            "document reads/writes and cannot be meaningfully mocked."
        ),
    ),
]


def _embedding(dimension: int = PYANNOTE_EMBEDDING_DIMENSION_V4) -> list[float]:
    return [round((i + 1) * 0.001, 6) for i in range(dimension)]


def _speaker_doc(speaker_uuid: str, user_id: int, name: str, **overrides) -> dict:
    base = {
        "speaker_id": 1,
        "speaker_uuid": speaker_uuid,
        "user_id": user_id,
        "name": name,
        "collection_ids": [],
        "segment_count": 1,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "embedding": _embedding(),
    }
    base.update(overrides)
    return base


@pytest.fixture
def indices(monkeypatch):
    """A throwaway speaker index (alias -> v4) AND a throwaway transcript index.

    ``find_speaker_across_media`` reads both planes, so both must be real —
    a stand-in for either would leave the join it performs (speaker name ->
    transcript ``speakers`` term match) completely unverified.
    """
    from app.core.config import settings
    from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4
    from app.services.opensearch_service import client as _client
    from app.services.opensearch_service.indices import _ensure_versioned_speaker_index

    client = _client.opensearch_client
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    base_name = f"test_speakers_meta_{uuid_pkg.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "OPENSEARCH_SPEAKER_INDEX", base_name)
    v3 = get_speaker_index_v3()
    v4 = get_speaker_index_v4()
    _ensure_versioned_speaker_index(v3, PYANNOTE_EMBEDDING_DIMENSION_V3)
    _ensure_versioned_speaker_index(v4, PYANNOTE_EMBEDDING_DIMENSION_V4)
    # `_ensure_versioned_speaker_index` swallows CLUSTER_UNAVAILABLE_ERRORS (which include 4xx
    # TransportErrors such as a shard-limit validation_exception) and returns without raising,
    # so under cluster pressure this fixture can "succeed" having created nothing. Fail here,
    # naming the cause, rather than three lines later as a mystery stale-document assertion.
    for name in (v3, v4):
        assert client.indices.exists(index=name), (
            f"index {name} was not created — _ensure_versioned_speaker_index swallowed the "
            "cluster error (issue #486)"
        )
    alias = get_speaker_index()
    client.indices.put_alias(index=v4, name=alias)

    transcript_index = f"test_transcripts_meta_{uuid_pkg.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", transcript_index)
    client.indices.create(
        index=transcript_index,
        body={
            "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
            "mappings": {
                "properties": {
                    "file_id": {"type": "integer"},
                    "file_uuid": {"type": "keyword"},
                    "user_id": {"type": "integer"},
                    "title": {"type": "text"},
                    "speakers": {"type": "keyword"},
                    "upload_time": {"type": "date"},
                }
            },
        },
    )

    try:
        yield SimpleNamespace(
            client=client, v3=v3, v4=v4, alias=alias, transcript_index=transcript_index
        )
    finally:
        for idx in (v3, v4, transcript_index):
            client.indices.delete(index=idx, ignore=[404])


# --------------------------------------------------------------------------- #
# find_speaker_across_media
# --------------------------------------------------------------------------- #


def test_find_speaker_across_media_returns_only_this_users_matching_files(indices):
    client = indices.client
    speaker_uuid = str(uuid_pkg.uuid4())
    user_id = 42
    client.index(
        index=indices.alias, id=speaker_uuid, body=_speaker_doc(speaker_uuid, user_id, "Dana")
    )

    matching_uuid = str(uuid_pkg.uuid4())
    other_speaker_uuid = str(uuid_pkg.uuid4())
    other_user_file_uuid = str(uuid_pkg.uuid4())
    client.index(
        index=indices.transcript_index,
        id="1",
        body={
            "file_id": 1,
            "file_uuid": matching_uuid,
            "user_id": user_id,
            "title": "Pricing sync",
            "speakers": ["Dana", "Ravi"],
            "upload_time": "2026-08-01T00:00:00+00:00",
        },
    )
    # Different speaker name entirely — excluded.
    client.index(
        index=indices.transcript_index,
        id="2",
        body={
            "file_id": 2,
            "file_uuid": other_speaker_uuid,
            "user_id": user_id,
            "title": "Unrelated meeting",
            "speakers": ["Ravi"],
            "upload_time": "2026-08-02T00:00:00+00:00",
        },
    )
    # Same speaker name, but a DIFFERENT user — must not leak across users.
    client.index(
        index=indices.transcript_index,
        id="3",
        body={
            "file_id": 3,
            "file_uuid": other_user_file_uuid,
            "user_id": 9999,
            "title": "Someone else's meeting",
            "speakers": ["Dana"],
            "upload_time": "2026-08-03T00:00:00+00:00",
        },
    )
    client.indices.refresh(index=indices.transcript_index)

    results = speaker_metadata.find_speaker_across_media(speaker_uuid, user_id)

    assert len(results) == 1
    assert results[0]["file_id"] == 1
    assert results[0]["file_uuid"] == matching_uuid
    assert results[0]["title"] == "Pricing sync"


def test_find_speaker_across_media_unknown_speaker_returns_empty(indices):
    results = speaker_metadata.find_speaker_across_media(str(uuid_pkg.uuid4()), 42)

    assert results == []


# --------------------------------------------------------------------------- #
# update_speaker_segment_count
# --------------------------------------------------------------------------- #


def test_update_speaker_segment_count_updates_the_stored_value(indices):
    speaker_uuid = str(uuid_pkg.uuid4())
    indices.client.index(
        index=indices.alias,
        id=speaker_uuid,
        body=_speaker_doc(speaker_uuid, 42, "SPEAKER_00", segment_count=1),
    )

    ok = speaker_metadata.update_speaker_segment_count(speaker_uuid, 77)

    assert ok is True
    doc = indices.client.get(index=indices.alias, id=speaker_uuid)["_source"]
    assert doc["segment_count"] == 77


def test_update_speaker_segment_count_missing_doc_returns_false(indices):
    ok = speaker_metadata.update_speaker_segment_count(str(uuid_pkg.uuid4()), 5)

    assert ok is False


# --------------------------------------------------------------------------- #
# update_speaker_display_name
# --------------------------------------------------------------------------- #


def test_update_speaker_display_name_sets_and_clears(indices):
    speaker_uuid = str(uuid_pkg.uuid4())
    indices.client.index(
        index=indices.alias,
        id=speaker_uuid,
        body=_speaker_doc(speaker_uuid, 42, "SPEAKER_00", display_name="Old Name"),
    )

    speaker_metadata.update_speaker_display_name(speaker_uuid, "New Name")
    doc = indices.client.get(index=indices.alias, id=speaker_uuid)["_source"]
    assert doc["display_name"] == "New Name"

    speaker_metadata.update_speaker_display_name(speaker_uuid, None)
    doc = indices.client.get(index=indices.alias, id=speaker_uuid)["_source"]
    assert doc["display_name"] is None


# --------------------------------------------------------------------------- #
# update_speaker_profile
# --------------------------------------------------------------------------- #


def test_update_speaker_profile_sets_profile_and_display_name(indices):
    speaker_uuid = str(uuid_pkg.uuid4())
    indices.client.index(
        index=indices.alias,
        id=speaker_uuid,
        body=_speaker_doc(speaker_uuid, 42, "SPEAKER_00", profile_id=None, verified=False),
    )

    speaker_metadata.update_speaker_profile(
        speaker_uuid,
        profile_id=5,
        profile_uuid="profile-abc",
        verified=True,
        display_name="Bob",
    )

    doc = indices.client.get(index=indices.alias, id=speaker_uuid)["_source"]
    assert doc["profile_id"] == 5
    assert doc["profile_uuid"] == "profile-abc"
    assert doc["verified"] is True
    assert doc["display_name"] == "Bob"


def test_update_speaker_profile_clears_assignment(indices):
    speaker_uuid = str(uuid_pkg.uuid4())
    indices.client.index(
        index=indices.alias,
        id=speaker_uuid,
        body=_speaker_doc(
            speaker_uuid, 42, "SPEAKER_00", profile_id=5, profile_uuid="profile-abc", verified=True
        ),
    )

    speaker_metadata.update_speaker_profile(speaker_uuid, profile_id=None, profile_uuid=None)

    doc = indices.client.get(index=indices.alias, id=speaker_uuid)["_source"]
    assert doc["profile_id"] is None
    assert doc["profile_uuid"] is None
    assert doc["verified"] is False


def test_update_speaker_profile_missing_doc_does_not_raise(indices):
    missing_uuid = str(uuid_pkg.uuid4())

    speaker_metadata.update_speaker_profile(missing_uuid, profile_id=1, profile_uuid="x")

    with pytest.raises(NotFoundError):
        indices.client.get(index=indices.alias, id=missing_uuid)


# --------------------------------------------------------------------------- #
# sync_speaker_profiles_to_opensearch
# --------------------------------------------------------------------------- #


def _media_file(db_session, user) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"sync-{uuid_pkg.uuid4().hex[:8]}.mp4",
        content_type="video/mp4",
        file_size=1000,
        storage_path=f"test/{uuid_pkg.uuid4().hex}.mp4",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def test_sync_speaker_profiles_updates_the_matching_opensearch_document(
    indices, db_session, normal_user
):
    """The query behind this sync is intentionally global (issue #474 note:
    the live dev DB already carries real speaker rows with a profile_id or
    display_name set — confirmed via ``SELECT count(*) FROM speaker WHERE
    profile_id IS NOT NULL OR display_name IS NOT NULL`` returning 12 at the
    time this test was written). So this test asserts on OUR document's
    before/after state, never on the aggregate return dict, which legitimately
    also reflects that unrelated live data.
    """
    media_file = _media_file(db_session, normal_user)
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        name="Carol Profile",
    )
    db_session.add(profile)
    db_session.flush()

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Carol",
        profile_id=profile.id,
        verified=True,
    )
    db_session.add(speaker)
    db_session.flush()

    speaker_uuid = str(speaker.uuid)
    indices.client.index(
        index=indices.alias,
        id=speaker_uuid,
        body=_speaker_doc(
            speaker_uuid,
            normal_user.id,
            "SPEAKER_00",
            profile_id=None,
            profile_uuid=None,
            display_name=None,
            verified=False,
        ),
    )

    result = speaker_metadata.sync_speaker_profiles_to_opensearch(db_session)

    assert result["updated"] >= 1
    doc = indices.client.get(index=indices.alias, id=speaker_uuid)["_source"]
    assert doc["profile_id"] == profile.id
    assert doc["profile_uuid"] == str(profile.uuid)
    assert doc["display_name"] == "Carol"
    assert doc["verified"] is True


def test_sync_speaker_profiles_counts_a_missing_opensearch_document_as_skipped(
    indices, db_session, normal_user
):
    """A Speaker row that qualifies (has a display_name) but was never indexed
    in OpenSearch must be reported as ``skipped``, not ``errors`` — an operator
    reading this count needs to know "nothing to fix here" from "something
    broke". Proven here against the REAL OpenSearch error text (this is
    exactly the kind of substring-matching fragility the task calls out:
    the source checks ``"document_missing_exception" in str(e)``, and only a
    live cluster's actual error string can confirm that match is real).
    """
    media_file = _media_file(db_session, normal_user)
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Never Indexed",
    )
    db_session.add(speaker)
    db_session.flush()
    # Deliberately no matching OpenSearch document for this speaker's uuid.

    baseline = speaker_metadata.sync_speaker_profiles_to_opensearch(db_session)

    assert baseline["skipped"] >= 1
    assert baseline["errors"] == 0, (
        "a genuinely missing document must classify as 'skipped', never 'errors' — "
        "if this fails, the 'document_missing_exception' substring match in the "
        "source no longer matches the live cluster's real error text"
    )


def test_sync_speaker_profiles_never_touches_a_speaker_with_no_profile_or_display_name(
    indices, db_session, normal_user
):
    """A Speaker row that does NOT qualify (no profile_id, no display_name)
    must never be visited at all — proven by planting a sentinel OpenSearch
    document under that speaker's uuid and confirming it survives untouched.
    """
    media_file = _media_file(db_session, normal_user)
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
    )
    db_session.add(speaker)
    db_session.flush()

    speaker_uuid = str(speaker.uuid)
    indices.client.index(
        index=indices.alias,
        id=speaker_uuid,
        body=_speaker_doc(
            speaker_uuid, normal_user.id, "SPEAKER_00", display_name="UNTOUCHED-SENTINEL"
        ),
    )

    speaker_metadata.sync_speaker_profiles_to_opensearch(db_session)

    doc = indices.client.get(index=indices.alias, id=speaker_uuid)["_source"]
    assert doc["display_name"] == "UNTOUCHED-SENTINEL"
