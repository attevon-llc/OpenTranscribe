"""Each voiceprint writer stamps the FILE's organization on the document it produces (#1280).

A purge no longer finds documents by their stamp (it uses Postgres ids), but the stamp still
decides which scope's search can match a document and which scope's write backstop judges it. A
writer that drops it turns an organization file's voiceprint into a personal one. One test per
writer, each driven through the real OpenSearch write function against a recording client, with
a personal-file control: a personal document must carry no ``organization_id`` field at all.

``test_voiceprint_writer_tenant_stamps.py`` covers the repair, migration-result, merge and rebuild
writers; these are the remaining four sites (the post-transcription bulk and profile writers, the
upload-time matcher, the profile recalculation and the migration's profile backfill).
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.models.media import SpeakerProfile
from app.services.opensearch_service import profiles as os_profiles
from app.services.opensearch_service import speaker_write
from app.tasks.transcription import embeddings as transcription_embeddings
from tests.fixtures.speaker_cleanup_seams import make_speaker

if TYPE_CHECKING:
    from app.tasks.transcription.context import TranscriptionContext

DIM = PYANNOTE_EMBEDDING_DIMENSION_V4


@pytest.fixture
def cluster(monkeypatch):
    """A recording stand-in for the OpenSearch client every write module shares."""
    from app.services.opensearch_service import client as _client

    fake = MagicMock(name="opensearch")
    fake.bulk.return_value = {"errors": False, "items": []}
    monkeypatch.setattr(_client, "opensearch_client", fake)
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    for module in (speaker_write, os_profiles):
        monkeypatch.setattr(module, "ensure_indices_exist", lambda *a, **k: None)
    return fake


def _stamped(doc: dict[str, Any], org_id: int | None) -> None:
    if org_id is None:
        assert "organization_id" not in doc, "a personal document carries no organization_id"
    else:
        assert doc["organization_id"] == org_id


def _vector() -> list[float]:
    return [0.5] * DIM


def _org(privacy_world, in_org: bool) -> int | None:
    return privacy_world.new_org().id if in_org else None


@contextmanager
def _session_of(db):
    yield db


@pytest.mark.parametrize("in_org", [True, False])
def test_the_post_transcription_bulk_writer_stamps_the_files_organization(
    privacy_world, cluster, normal_user, monkeypatch, in_org
):
    org_id = _org(privacy_world, in_org)
    media_file = privacy_world.new_file(normal_user, org_id)
    speaker = make_speaker(privacy_world.db, normal_user, media_file)
    privacy_world.db.commit()
    monkeypatch.setattr(
        transcription_embeddings, "session_scope", lambda: _session_of(privacy_world.db)
    )
    monkeypatch.setattr("app.services.opensearch_service.ensure_v4_index_exists", lambda: True)
    ctx = cast(
        "TranscriptionContext",
        SimpleNamespace(
            file_id=media_file.id,
            user_id=normal_user.id,
            organization_id=media_file.organization_id,
        ),
    )

    transcription_embeddings._store_native_centroids_in_v4_staging(
        ctx, {"SPEAKER_00": np.array(_vector())}, {"SPEAKER_00": speaker.id}
    )

    (call,) = cluster.bulk.call_args_list
    docs = call.kwargs["body"][1::2]
    assert [d["speaker_uuid"] for d in docs] == [str(speaker.uuid)]
    _stamped(docs[0], org_id)


@pytest.mark.parametrize("in_org", [True, False])
def test_the_post_transcription_profile_writer_stamps_the_profiles_organization(
    privacy_world, cluster, normal_user, monkeypatch, in_org
):
    org_id = _org(privacy_world, in_org)
    profile = SpeakerProfile(
        user_id=normal_user.id, organization_id=org_id, name=f"p-{uuid_pkg.uuid4().hex[:6]}"
    )
    privacy_world.db.add(profile)
    privacy_world.db.commit()
    monkeypatch.setattr(
        transcription_embeddings, "session_scope", lambda: _session_of(privacy_world.db)
    )
    monkeypatch.setattr(
        transcription_embeddings, "_fetch_existing_v4_embeddings", lambda *a, **k: []
    )

    media_file = privacy_world.new_file(normal_user, org_id)
    speaker = make_speaker(privacy_world.db, normal_user, media_file, profile_id=profile.id)
    privacy_world.db.commit()

    updated = transcription_embeddings._update_v4_profile_embeddings(
        {profile.id}, {"SPEAKER_00": np.array(_vector())}, {"SPEAKER_00": speaker.id}, set()
    )

    assert updated == 1
    docs = [c.kwargs["body"] for c in cluster.index.call_args_list]
    (doc,) = [d for d in docs if d.get("document_type") == "profile"]
    assert doc["profile_uuid"] == str(profile.uuid)
    _stamped(doc, org_id)


@pytest.mark.parametrize("in_org", [True, False])
def test_the_upload_time_matcher_stamps_the_files_organization(
    privacy_world, cluster, normal_user, in_org
):
    from app.services.speaker_matching_service import SpeakerMatchingService

    org_id = _org(privacy_world, in_org)
    media_file = privacy_world.new_file(normal_user, org_id)
    speaker = make_speaker(privacy_world.db, normal_user, media_file)
    privacy_world.db.commit()
    match = {"confidence": 0.5, "suggested_name": "Ann", "auto_accept": False, "profile_id": None}

    with (
        patch.object(SpeakerMatchingService, "find_and_store_speaker_matches"),
        patch.object(SpeakerMatchingService, "match_speaker_to_known_speakers", return_value=match),
        patch("app.tasks.rename_propagation_task.propagate_speaker_rename.delay"),
    ):
        SpeakerMatchingService(privacy_world.db, None).process_speaker_embeddings_native(
            media_file_id=int(media_file.id),
            user_id=int(normal_user.id),
            native_embeddings={int(speaker.id): np.array(_vector(), dtype=np.float32)},
        )

    (doc,) = [c.kwargs["body"] for c in cluster.index.call_args_list]
    assert doc["speaker_uuid"] == str(speaker.uuid)
    _stamped(doc, org_id)


@pytest.mark.parametrize("in_org", [True, False])
def test_the_profile_recalculation_stamps_the_profiles_organization(
    privacy_world, cluster, normal_user, in_org
):
    from app.services import profile_embedding_service

    org_id = _org(privacy_world, in_org)
    profile = SpeakerProfile(
        user_id=normal_user.id, organization_id=org_id, name=f"p-{uuid_pkg.uuid4().hex[:6]}"
    )
    privacy_world.db.add(profile)
    privacy_world.db.commit()

    assert profile_embedding_service._process_profile_with_embeddings(
        profile, int(profile.id), [_vector(), _vector()]
    )

    (doc,) = [c.kwargs["body"] for c in cluster.index.call_args_list]
    assert doc["document_type"] == "profile" and doc["profile_uuid"] == str(profile.uuid)
    _stamped(doc, org_id)


@pytest.mark.parametrize("in_org", [True, False])
def test_the_migration_profile_backfill_stamps_the_organization_of_the_documents_it_averages(
    privacy_world, cluster, normal_user, monkeypatch, in_org
):
    from app.tasks import embedding_migration_v4 as migration

    org_id = _org(privacy_world, in_org)
    profile = SpeakerProfile(
        user_id=normal_user.id, organization_id=org_id, name=f"p-{uuid_pkg.uuid4().hex[:6]}"
    )
    privacy_world.db.add(profile)
    privacy_world.db.commit()
    source = {
        "profile_id": profile.id,
        "profile_uuid": str(profile.uuid),
        "user_id": normal_user.id,
        "segment_count": 1,
        "embedding": _vector(),
    }
    if org_id is not None:
        source["organization_id"] = org_id
    cluster.search.return_value = {"_scroll_id": None, "hits": {"hits": [{"_source": source}]}}
    monkeypatch.setattr(migration, "session_scope", lambda: _session_of(privacy_world.db))

    written = migration.migrate_profile_documents_to_v4(cluster, "speakers_v4")

    assert written == 1
    (doc,) = [c.kwargs["body"] for c in cluster.index.call_args_list]
    assert doc["document_type"] == "profile"
    _stamped(doc, org_id)
