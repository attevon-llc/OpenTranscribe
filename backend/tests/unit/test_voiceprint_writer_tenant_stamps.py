"""Every writer that rebuilds a voiceprint document keeps the tenant of the file it came from (#1280).

A speaker voiceprint is written as a WHOLE document, so a writer that does not pass the file's
organization produces one that reads as personal. Three things then go wrong at once: the write
backstop judges the wrong scope, the personal-scope purge deletes an organization's voiceprint,
and the organization's own purge (which matches on the stamp) does not find it.

Each writer is driven through the REAL OpenSearch write function against a recording client, so
the assertion is on the document that would have been indexed, never on a call signature. Every
case has its personal-file control: a personal document must carry no ``organization_id`` field
at all (the personal search gate matches on its absence).
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.services.opensearch_service import speaker_write
from app.tasks import embedding_consistency_repair as repair_task
from app.tasks import embedding_migration_v4 as migration_task
from app.tasks import speaker_merge_task
from app.tasks.migration_pipeline import PreparedFile
from app.tasks.migration_pipeline import SpeakerSnapshot

DIM = PYANNOTE_EMBEDDING_DIMENSION_V4


@pytest.fixture
def cluster(monkeypatch):
    """A recording stand-in for the OpenSearch client every write module shares."""
    from app.services.opensearch_service import client as _client

    fake = MagicMock(name="opensearch")
    monkeypatch.setattr(_client, "opensearch_client", fake)
    monkeypatch.setattr(speaker_write, "ensure_indices_exist", lambda *a, **k: None)
    return fake


def _indexed(cluster) -> list[dict[str, Any]]:
    """Every document handed to ``index`` (one speaker write each)."""
    return [call.kwargs["body"] for call in cluster.index.call_args_list]


@contextmanager
def _session_of(db):
    yield db


def _prepared(org_id: int | None, user_id: int, speaker_uuid: str) -> PreparedFile:
    return PreparedFile(
        file_uuid=str(uuid_pkg.uuid4()),
        audio_source="s3://bucket/key",
        speakers=[SpeakerSnapshot(id=1, uuid=speaker_uuid, name="SPEAKER_00")],
        speaker_segments={},
        media_file_id=1,
        user_id=user_id,
        organization_id=org_id,
    )


def _results() -> dict[str, list[Any]]:
    value = np.array([0.5] * DIM)
    return {"embedding": [SimpleNamespace(speaker_id=1, value=value)]}


# --- the consistency repair's two writers -------------------------------------------------


@pytest.mark.parametrize("writer_name", ["_v3_result_writer", "_v4_result_writer"])
@pytest.mark.parametrize("in_org", [True, False])
def test_the_repair_writers_stamp_an_organization_file_and_leave_a_personal_one_unstamped(
    privacy_world, cluster, normal_user, monkeypatch, writer_name, in_org
):
    org_id = privacy_world.new_org().id if in_org else None
    monkeypatch.setattr(repair_task, "_voiceprints_allowed", lambda _file_id: True)
    speaker_uuid = str(uuid_pkg.uuid4())
    prepared = _prepared(org_id, normal_user.id, speaker_uuid)

    written = getattr(repair_task, writer_name)(prepared, _results(), {speaker_uuid})

    assert written == 1
    (doc,) = _indexed(cluster)
    if in_org:
        assert doc["organization_id"] == org_id
    else:
        assert "organization_id" not in doc


@pytest.mark.parametrize("writer_name", ["_v3_result_writer", "_v4_result_writer"])
def test_the_write_backstop_judges_the_files_organization_not_the_owners_personal_scope(
    privacy_world, cluster, normal_user, monkeypatch, writer_name
):
    """The entry gate is made to pass (a switch flipped between gate and write): the backstop is
    the last line, and it must read the ORGANIZATION's switch for an organization's file."""
    org = privacy_world.new_org()
    privacy_world.set_org(org.id, voiceprints=False)  # the owner's personal scope stays ON
    monkeypatch.setattr(repair_task, "_voiceprints_allowed", lambda _file_id: True)
    speaker_uuid = str(uuid_pkg.uuid4())

    written = getattr(repair_task, writer_name)(
        _prepared(org.id, normal_user.id, speaker_uuid), _results(), {speaker_uuid}
    )

    assert written == 0
    assert _indexed(cluster) == []


# --- the v4 migration's bulk writer -------------------------------------------------------


@pytest.mark.parametrize("in_org", [True, False])
def test_the_v4_migration_stamps_the_documents_it_bulk_writes(
    privacy_world, normal_user, monkeypatch, in_org
):
    org_id = privacy_world.new_org().id if in_org else None
    monkeypatch.setattr(migration_task, "_file_skipped_by_policy", lambda _file_id: False)
    docs: list[dict[str, Any]] = []

    def record(batch: list[dict[str, Any]]) -> int:
        docs.extend(batch)
        return len(batch)

    monkeypatch.setattr(migration_task, "_bulk_write_v4_embeddings", record)
    speaker_uuid = str(uuid_pkg.uuid4())

    migration_task._embedding_result_writer(
        _prepared(org_id, normal_user.id, speaker_uuid), _results()
    )

    (doc,) = docs
    if in_org:
        assert doc["organization_id"] == org_id
    else:
        assert "organization_id" not in doc


# --- the speaker merge's averaged voiceprint ----------------------------------------------


@pytest.mark.parametrize("in_org", [True, False])
def test_a_merged_speakers_averaged_voiceprint_keeps_the_files_organization(
    privacy_world, cluster, normal_user, monkeypatch, in_org
):
    from app.api.endpoints.speakers import _merge_speaker_embeddings
    from tests.fixtures.speaker_cleanup_seams import make_speaker

    org_id = privacy_world.new_org().id if in_org else None
    target = make_speaker(
        privacy_world.db,
        normal_user,
        privacy_world.new_file(normal_user, org_id),
    )
    privacy_world.db.commit()

    @contextmanager
    def scope():
        yield privacy_world.db

    monkeypatch.setattr(speaker_merge_task, "session_scope", scope)
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embedding", lambda _uuid: [0.01] * DIM
    )
    plan = speaker_merge_task._load_merge_plan(str(target.uuid))
    assert plan is not None

    _merge_speaker_embeddings(str(uuid_pkg.uuid4()), plan)

    (doc,) = _indexed(cluster)
    assert doc["speaker_uuid"] == str(target.uuid)
    if in_org:
        assert doc["organization_id"] == org_id
    else:
        assert "organization_id" not in doc


# --- the last-resort index rebuild --------------------------------------------------------


def test_the_index_rebuild_carries_each_documents_organization_through(
    db_session, normal_user, monkeypatch
):
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import SpeakerCluster
    from app.services.opensearch_service import repair

    cl = SpeakerCluster(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, label="r", member_count=1)
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="rebuild-stamp.mp4",
        content_type="video/mp4",
        file_size=1000,
        storage_path=f"rebuild-stamp/{uuid_pkg.uuid4().hex}",
    )
    db_session.add_all([cl, media])
    db_session.flush()
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        name="rebuild-stamp",
        user_id=normal_user.id,
        media_file_id=media.id,
        cluster_id=cl.id,
    )
    db_session.add(speaker)
    db_session.flush()
    speaker_uuid = str(speaker.uuid)

    stored = {
        "speaker_uuid": speaker_uuid,
        "speaker_id": 1,
        "user_id": normal_user.id,
        "name": "spk",
        "organization_id": 77,
        "embedding": [0.1, 0.2],
    }
    page = {"hits": {"hits": [{"_id": speaker_uuid, "sort": ["a"], "_source": stored}]}}
    empty: dict[str, Any] = {"hits": {"hits": []}}
    client = MagicMock()
    client.indices.exists.side_effect = lambda index, **_k: index in ("speakers", "speakers_v4")
    client.search.side_effect = [page, empty, page, empty]
    client.bulk.return_value = {"errors": False}
    client.count.return_value = {"count": 1}
    monkeypatch.setattr("app.services.opensearch_service.client.opensearch_client", client)
    monkeypatch.setattr(repair, "get_speaker_embedding_dimension", lambda: 2)

    result = repair.rebuild_speaker_index(db_session)

    assert result["status"] == "rebuilt"
    # The scan asked for the stamp...
    assert "organization_id" in client.search.call_args_list[0].kwargs["body"]["_source"]
    # ...and the document loaded into the rebuild index still carries it.
    loaded = client.bulk.call_args_list[0].kwargs["body"][1]
    assert loaded["organization_id"] == 77


# --- where the stamp comes from -----------------------------------------------------------


@pytest.mark.parametrize("in_org", [True, False])
def test_a_prepared_file_carries_the_organization_of_its_media_file_row(
    privacy_world, normal_user, monkeypatch, in_org
):
    from app.models.media import TranscriptSegment
    from app.tasks import migration_pipeline
    from tests.fixtures.speaker_cleanup_seams import make_speaker

    org_id = privacy_world.new_org().id if in_org else None
    media_file = privacy_world.new_file(normal_user, org_id)
    speaker = make_speaker(privacy_world.db, normal_user, media_file)
    privacy_world.db.add(
        TranscriptSegment(
            media_file_id=media_file.id, speaker_id=speaker.id, start_time=0, end_time=5, text="x"
        )
    )
    privacy_world.db.commit()
    monkeypatch.setattr(migration_pipeline, "session_scope", lambda: _session_of(privacy_world.db))
    fake_storage = MagicMock()
    fake_storage.presigned_get_object.return_value = "https://storage.invalid/audio"
    monkeypatch.setattr("app.services.minio_service.minio_client", fake_storage)

    prepared = migration_pipeline.prepare_file(str(media_file.uuid))

    assert prepared is not None
    assert prepared.organization_id == org_id


# --- the consequence: which purge finds the document --------------------------------------


def test_an_organizations_repaired_voiceprint_goes_with_its_purge_and_not_the_personal_one(
    privacy_world, cluster, normal_user, monkeypatch
):
    """The purge selects documents by the ids of the scope's speakers (Postgres), so the stamp
    the writer puts on the document no longer decides which purge finds it; the stamp is still
    pinned above because the read gates and the write backstop depend on it."""
    from app.services.privacy.purge import PurgeScope
    from app.services.privacy.purge_voiceprints import VoiceprintTargets
    from app.services.privacy.purge_voiceprints import purge_voiceprint_docs
    from tests.fixtures.fake_speaker_index import FakeOpenSearch
    from tests.fixtures.speaker_cleanup_seams import make_speaker

    org = privacy_world.new_org()
    speaker = make_speaker(
        privacy_world.db, normal_user, privacy_world.new_file(normal_user, org.id)
    )
    privacy_world.db.commit()
    monkeypatch.setattr(repair_task, "_voiceprints_allowed", lambda _file_id: True)
    speaker_uuid = str(speaker.uuid)
    repair_task._v4_result_writer(
        _prepared(org.id, normal_user.id, speaker_uuid), _results(), {speaker_uuid}
    )
    (doc,) = _indexed(cluster)
    assert doc["organization_id"] == org.id
    search = FakeOpenSearch()
    search.add(speaker_uuid, doc["user_id"], doc.get("organization_id"))
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", search)

    personal = PurgeScope("user", user_id=normal_user.id, organization_id=None)
    nothing = VoiceprintTargets()  # a personal purge selects no speaker of this organization file
    assert purge_voiceprint_docs(personal, nothing, [])["speaker_docs_deleted"] == 0
    assert search.ids() == {speaker_uuid}

    in_org = VoiceprintTargets(speaker_uuids=[speaker_uuid])
    organization = PurgeScope("organization", organization_id=org.id)
    assert purge_voiceprint_docs(organization, in_org, [])["speaker_docs_deleted"] == 1
    assert search.ids() == set()
