"""``opensearch_service.speaker_maintenance`` — merges, removal and orphan sweeps.

Real writes/deletes against a throwaway pair of speaker indices on the live
dev OpenSearch cluster (see ``test_speaker_write.py`` for why: this module's
callers are all live per the ``rg`` check in issue #474's Priority 1 list).
``cleanup_orphaned_speaker_embeddings`` also needs Postgres — its own
``session_scope()`` is patched to hand out the savepoint-rolled-back
``db_session`` (the ``test_dispatch.py`` pattern), since it is a fresh
``from app.db.session_utils import session_scope`` bound at call time, not a
module attribute this file can patch directly.

No kNN ``_score`` is ever read in this module (only ``get``/``search``/
``update``/``delete`` by id or term), so the cosinesimil conversion rule in
``app/services/search/CLAUDE.md`` does not apply here — confirmed by reading
every OpenSearch call site in ``speaker_maintenance.py``.
"""

from __future__ import annotations

import datetime
import os
import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import NotFoundError

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.models.media import MediaFile
from app.services.opensearch_service import speaker_maintenance

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
            "document writes/deletes and cannot be meaningfully mocked."
        ),
    ),
]


def _embedding(dimension: int, offset: float = 0.0) -> list[float]:
    return [round(((i + 1) * 0.001 + offset) % 1.0, 6) for i in range(dimension)]


@pytest.fixture
def speaker_indices(monkeypatch):
    """Throwaway v3/v4 speaker indices with the alias pointing at v4 (fresh-install shape)."""
    from app.core.config import settings
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4
    from app.services.opensearch_service import client as _client
    from app.services.opensearch_service.indices import _ensure_versioned_speaker_index

    client = _client.opensearch_client
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    base_name = f"test_speakers_maint_{uuid_pkg.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "OPENSEARCH_SPEAKER_INDEX", base_name)

    v3 = get_speaker_index_v3()
    v4 = get_speaker_index_v4()
    _ensure_versioned_speaker_index(v3, PYANNOTE_EMBEDDING_DIMENSION_V3)
    _ensure_versioned_speaker_index(v4, PYANNOTE_EMBEDDING_DIMENSION_V4)
    alias = get_speaker_index()
    client.indices.put_alias(index=v4, name=alias)

    try:
        yield SimpleNamespace(client=client, v3=v3, v4=v4, alias=alias)
    finally:
        for idx in (v3, v4):
            client.indices.delete(index=idx, ignore=[404])


@pytest.fixture
def speaker_indices_alias_v3(monkeypatch):
    """Variant where the alias resolves to v3 — the mid-migration shape.

    ``merge_speaker_embeddings`` treats the alias (main index) and
    ``speakers_v4`` (migration staging) as two independent locations. That
    distinction only shows up when they are genuinely different concrete
    indices, which the default (alias -> v4) fixture above cannot exercise.
    """
    from app.core.config import settings
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4
    from app.services.opensearch_service import client as _client
    from app.services.opensearch_service.indices import _ensure_versioned_speaker_index

    client = _client.opensearch_client
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    base_name = f"test_speakers_maint_v3_{uuid_pkg.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "OPENSEARCH_SPEAKER_INDEX", base_name)

    v3 = get_speaker_index_v3()
    v4 = get_speaker_index_v4()
    _ensure_versioned_speaker_index(v3, PYANNOTE_EMBEDDING_DIMENSION_V3)
    _ensure_versioned_speaker_index(v4, PYANNOTE_EMBEDDING_DIMENSION_V4)
    alias = get_speaker_index()
    client.indices.put_alias(index=v3, name=alias)

    try:
        yield SimpleNamespace(client=client, v3=v3, v4=v4, alias=alias)
    finally:
        for idx in (v3, v4):
            client.indices.delete(index=idx, ignore=[404])


def _doc(speaker_uuid: str, user_id: int, **overrides) -> dict:
    base = {
        "speaker_id": 1,
        "speaker_uuid": speaker_uuid,
        "user_id": user_id,
        "name": "SPEAKER_00",
        "collection_ids": [],
        "segment_count": 1,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "embedding": _embedding(PYANNOTE_EMBEDDING_DIMENSION_V4),
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# remove_speaker_embedding
# --------------------------------------------------------------------------- #


def test_remove_speaker_embedding_removes_from_every_index_it_is_present_in(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())
    client = speaker_indices.client
    client.index(
        index=speaker_indices.v3,
        id=speaker_uuid,
        body=_doc(speaker_uuid, 42, embedding=_embedding(PYANNOTE_EMBEDDING_DIMENSION_V3)),
    )
    client.index(index=speaker_indices.v4, id=speaker_uuid, body=_doc(speaker_uuid, 42))

    success = speaker_maintenance.remove_speaker_embedding(speaker_uuid)

    assert success is True
    with pytest.raises(NotFoundError):
        client.get(index=speaker_indices.v3, id=speaker_uuid)
    with pytest.raises(NotFoundError):
        client.get(index=speaker_indices.v4, id=speaker_uuid)


def test_remove_speaker_embedding_partial_presence_still_reports_success(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())
    client = speaker_indices.client
    client.index(index=speaker_indices.v4, id=speaker_uuid, body=_doc(speaker_uuid, 42))

    success = speaker_maintenance.remove_speaker_embedding(speaker_uuid)

    assert success is True
    with pytest.raises(NotFoundError):
        client.get(index=speaker_indices.v4, id=speaker_uuid)


def test_remove_speaker_embedding_absent_everywhere_returns_false(speaker_indices):
    never_indexed = str(uuid_pkg.uuid4())

    success = speaker_maintenance.remove_speaker_embedding(never_indexed)

    assert success is False


# --------------------------------------------------------------------------- #
# merge_speaker_embeddings
# --------------------------------------------------------------------------- #


def test_merge_deletes_source_and_updates_target_collections(speaker_indices):
    source_uuid = str(uuid_pkg.uuid4())
    target_uuid = str(uuid_pkg.uuid4())
    client = speaker_indices.client
    client.index(
        index=speaker_indices.alias,
        id=source_uuid,
        body=_doc(source_uuid, 42, collection_ids=[1]),
    )
    client.index(
        index=speaker_indices.alias,
        id=target_uuid,
        body=_doc(target_uuid, 42, collection_ids=[2], updated_at="2020-01-01T00:00:00+00:00"),
    )

    response = speaker_maintenance.merge_speaker_embeddings(source_uuid, target_uuid, [1, 2, 3])

    assert response is not None
    with pytest.raises(NotFoundError):
        client.get(index=speaker_indices.v4, id=source_uuid)

    target_doc = client.get(index=speaker_indices.v4, id=target_uuid)["_source"]
    assert target_doc["collection_ids"] == [1, 2, 3]
    assert target_doc["updated_at"] != "2020-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# app.api.endpoints.speakers._update_opensearch_speaker_merge (issue #474)
# --------------------------------------------------------------------------- #


def test_update_opensearch_speaker_merge_preserves_target_collection_membership(
    speaker_indices,
):
    """Regression test: ``_update_opensearch_speaker_merge`` used to call
    ``merge_speaker_embeddings(..., [])`` unconditionally, which
    ``merge_speaker_embeddings`` writes verbatim into the target's
    ``collection_ids`` — silently stripping the surviving speaker out of every
    OpenSearch collection it belonged to on every merge. The caller must now
    read the target's CURRENT collection_ids and pass those through instead.
    """
    from app.api.endpoints.speakers import _update_opensearch_speaker_merge

    source_uuid = str(uuid_pkg.uuid4())
    target_uuid = str(uuid_pkg.uuid4())
    client = speaker_indices.client
    client.index(index=speaker_indices.alias, id=source_uuid, body=_doc(source_uuid, 42))
    client.index(
        index=speaker_indices.alias,
        id=target_uuid,
        body=_doc(target_uuid, 42, collection_ids=[7, 8]),
    )
    client.indices.refresh(index=speaker_indices.alias)

    _update_opensearch_speaker_merge(source_uuid, target_uuid)

    target_doc = client.get(index=speaker_indices.v4, id=target_uuid)["_source"]
    assert target_doc["collection_ids"] == [7, 8]
    with pytest.raises(NotFoundError):
        client.get(index=speaker_indices.v4, id=source_uuid)


def test_update_opensearch_speaker_merge_defaults_to_empty_when_target_has_none(
    speaker_indices,
):
    from app.api.endpoints.speakers import _update_opensearch_speaker_merge

    source_uuid = str(uuid_pkg.uuid4())
    target_uuid = str(uuid_pkg.uuid4())
    client = speaker_indices.client
    client.index(index=speaker_indices.alias, id=source_uuid, body=_doc(source_uuid, 42))
    client.index(
        index=speaker_indices.alias, id=target_uuid, body=_doc(target_uuid, 42, collection_ids=[])
    )
    client.indices.refresh(index=speaker_indices.alias)

    _update_opensearch_speaker_merge(source_uuid, target_uuid)

    target_doc = client.get(index=speaker_indices.v4, id=target_uuid)["_source"]
    assert target_doc["collection_ids"] == []


def test_merge_also_removes_the_source_from_v4_staging(speaker_indices_alias_v3):
    """Migration-in-progress shape: the alias resolves to v3, but a leftover
    copy of the source speaker sits in the v4 staging index too."""
    fixture = speaker_indices_alias_v3
    source_uuid = str(uuid_pkg.uuid4())
    target_uuid = str(uuid_pkg.uuid4())
    client = fixture.client
    v3_embedding = _embedding(PYANNOTE_EMBEDDING_DIMENSION_V3)
    client.index(
        index=fixture.alias, id=source_uuid, body=_doc(source_uuid, 42, embedding=v3_embedding)
    )
    client.index(
        index=fixture.alias, id=target_uuid, body=_doc(target_uuid, 42, embedding=v3_embedding)
    )
    # Stray v4-staging copy of the source, from an in-progress migration.
    client.index(
        index=fixture.v4,
        id=source_uuid,
        body=_doc(source_uuid, 42, embedding=_embedding(PYANNOTE_EMBEDDING_DIMENSION_V4)),
    )

    speaker_maintenance.merge_speaker_embeddings(source_uuid, target_uuid, [9])

    with pytest.raises(NotFoundError):
        client.get(index=fixture.v3, id=source_uuid)
    with pytest.raises(NotFoundError):
        client.get(index=fixture.v4, id=source_uuid)


# --------------------------------------------------------------------------- #
# cleanup_orphaned_speaker_embeddings (real Postgres + real OpenSearch)
# --------------------------------------------------------------------------- #


@contextmanager
def _yield_session(db):
    yield db


def test_cleanup_orphaned_speaker_embeddings_deletes_only_true_orphans(
    speaker_indices, db_session, normal_user
):
    """The headline behavior: a speaker doc whose media_file_id no longer
    exists in Postgres is deleted; one whose file still exists is kept; a
    profile document (``document_type`` present) is never even considered,
    orphaned or not.
    """
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="keep-me.mp4",
        content_type="video/mp4",
        file_size=1000,
        storage_path=f"test/{uuid_pkg.uuid4().hex}.mp4",
    )
    db_session.add(media_file)
    db_session.flush()

    client = speaker_indices.client
    kept_uuid = str(uuid_pkg.uuid4())
    orphan_uuid = str(uuid_pkg.uuid4())
    profile_doc_uuid = str(uuid_pkg.uuid4())

    client.index(
        index=speaker_indices.alias,
        id=kept_uuid,
        body=_doc(kept_uuid, normal_user.id, media_file_id=media_file.id),
    )
    client.index(
        index=speaker_indices.alias,
        id=orphan_uuid,
        body=_doc(orphan_uuid, normal_user.id, media_file_id=999_999_999),
    )
    client.index(
        index=speaker_indices.alias,
        id=profile_doc_uuid,
        body=_doc(
            profile_doc_uuid,
            normal_user.id,
            media_file_id=999_999_999,
            document_type="profile",
        ),
    )
    client.indices.refresh(index=speaker_indices.alias)

    with patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)):
        deleted_count = speaker_maintenance.cleanup_orphaned_speaker_embeddings(normal_user.id)

    assert deleted_count == 1
    # Kept: file still exists.
    assert client.get(index=speaker_indices.alias, id=kept_uuid)["_source"]["media_file_id"] == (
        media_file.id
    )
    # Deleted: orphaned.
    with pytest.raises(NotFoundError):
        client.get(index=speaker_indices.alias, id=orphan_uuid)
    # Untouched: it's a profile document, excluded from the query entirely.
    assert client.get(index=speaker_indices.alias, id=profile_doc_uuid) is not None


def test_cleanup_orphaned_speaker_embeddings_no_orphans_deletes_nothing(
    speaker_indices, db_session, normal_user
):
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="keep-me-2.mp4",
        content_type="video/mp4",
        file_size=1000,
        storage_path=f"test/{uuid_pkg.uuid4().hex}.mp4",
    )
    db_session.add(media_file)
    db_session.flush()

    client = speaker_indices.client
    kept_uuid = str(uuid_pkg.uuid4())
    client.index(
        index=speaker_indices.alias,
        id=kept_uuid,
        body=_doc(kept_uuid, normal_user.id, media_file_id=media_file.id),
    )
    client.indices.refresh(index=speaker_indices.alias)

    with patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)):
        deleted_count = speaker_maintenance.cleanup_orphaned_speaker_embeddings(normal_user.id)

    assert deleted_count == 0
    assert client.get(index=speaker_indices.alias, id=kept_uuid) is not None
