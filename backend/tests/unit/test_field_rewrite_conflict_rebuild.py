"""A field rewrite that loses documents to version conflicts queues the file's rebuild.

The tag, access and rename rewrites are ``update_by_query`` calls with
``conflicts="proceed"``: a document another writer changed after the call's snapshot is
skipped and only counted in ``version_conflicts``. Renaming a speaker and adding a tag in the
same second makes two rewrites of the same chunk documents (each carries the file-level
``speakers`` and ``tags``), so one of them lost and search kept the old value until a manual
``POST /api/search/reindex``. The OpenSearch client is faked; the dispatch boundary,
``queue_transcript_reindex``, is recorded.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from fixtures.tag_world import make_file

from app.services.privacy import policy
from app.services.search.reindex_dispatch import ReindexDispatch
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import build_world


class _Indices:
    @staticmethod
    def refresh(**kwargs: Any) -> dict[str, Any]:
        return {}


class _ConflictingClient:
    """Every rewrite skips ``conflicts`` documents."""

    def __init__(self, conflicts: int) -> None:
        self.conflicts = conflicts
        self.calls = 0
        self.indices = _Indices()

    def update_by_query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"updated": 1, "version_conflicts": self.conflicts}


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def seams(monkeypatch, db_session):
    """The tasks' session, the bump and the reindex dispatch; returns the dispatch log."""
    rebuilds: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def scope():
        yield db_session

    def queue(**kwargs: Any) -> ReindexDispatch:
        rebuilds.append(kwargs)
        return ReindexDispatch.QUEUED

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr("app.services.search.reindex_dispatch.queue_transcript_reindex", queue)
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", lambda: None)
    return rebuilds


def _use(monkeypatch, client: _ConflictingClient) -> None:
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: client)


def _identity(media_file: Any, owner: Any) -> dict[str, Any]:
    return {"file_id": media_file.id, "file_uuid": str(media_file.uuid), "user_id": owner.id}


def test_a_tag_rewrite_that_hit_conflicts_queues_the_file_rebuild(
    db_session, normal_user, monkeypatch, seams
):
    from app.tasks.search_file_field_tasks import update_file_tags_index

    media_file = make_file(db_session, normal_user)
    _use(monkeypatch, _ConflictingClient(conflicts=3))

    result = update_file_tags_index([media_file.id])

    assert seams == [_identity(media_file, normal_user)]
    assert result["rebuild_queued"] == [media_file.id]


def test_a_clean_tag_rewrite_queues_nothing(db_session, normal_user, monkeypatch, seams):
    from app.tasks.search_file_field_tasks import update_file_tags_index

    media_file = make_file(db_session, normal_user)
    _use(monkeypatch, _ConflictingClient(conflicts=0))

    result = update_file_tags_index([media_file.id])

    assert result == {"status": "success", "updated": 1, "files": 1, "errors": 0}
    assert seams == []


def test_an_access_rewrite_that_hit_conflicts_queues_the_file_rebuild(
    db_session, normal_user, monkeypatch, seams
):
    """A share revoked during another rewrite must not stay searchable by the old grantee."""
    from app.tasks.search_file_field_tasks import update_file_access_index

    media_file = make_file(db_session, normal_user)
    _use(monkeypatch, _ConflictingClient(conflicts=2))

    result = update_file_access_index([media_file.id])

    assert seams == [_identity(media_file, normal_user)]
    assert result["rebuild_queued"] == [media_file.id]


def test_a_rename_still_conflicting_after_its_retries_queues_the_file_rebuild(
    db_session, normal_user, monkeypatch, seams
):
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )
    client = _ConflictingClient(conflicts=4)
    _use(monkeypatch, client)

    propagate_speaker_rename.apply(
        kwargs={
            "file_uuid": str(world.file.uuid),
            "old_names": ["SPEAKER_01"],
            "new_name": "Dana Renamed",
        }
    )

    assert client.calls == propagate_speaker_rename.max_retries + 1
    assert seams == [_identity(world.file, normal_user)]


def test_a_title_rename_still_conflicting_after_its_retries_queues_the_file_rebuild(
    db_session, normal_user, monkeypatch, seams
):
    from app.tasks.rename_propagation_task import propagate_title_rename

    media_file = make_file(db_session, normal_user)
    client = _ConflictingClient(conflicts=1)
    _use(monkeypatch, client)

    propagate_title_rename.apply(
        kwargs={"file_uuid": str(media_file.uuid), "new_title": str(media_file.filename)}
    )

    assert client.calls == propagate_title_rename.max_retries + 1
    assert seams == [_identity(media_file, normal_user)]
