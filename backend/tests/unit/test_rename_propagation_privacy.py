"""A speaker rename while the file's voiceprints are off rebuilds the file (#1280).

``propagate_speaker_rename`` rewrites the label in place with an ``update_by_query`` keyed on
the names the index was written with. While the file's voiceprints are off the index may hold
a label the policy now hides, so a filter computed without that knowledge matches nothing and
the task would report success over a stale index. It queues the file's reindex instead and
leaves the in-place rewrite for files whose voiceprints are on.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from app.services.privacy import policy
from app.services.search.reindex_dispatch import ReindexDispatch
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import build_world


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def seams(monkeypatch, db_session):
    """The task's session, the OpenSearch client and the reindex dispatch, all recorded."""
    updates: list[dict[str, Any]] = []
    reindexes: list[dict[str, Any]] = []

    class FakeClient:
        def update_by_query(self, **kwargs):
            updates.append(kwargs)
            return {"updated": 1, "version_conflicts": 0}

        class indices:  # noqa: N801 — mirrors the client's attribute shape
            @staticmethod
            def refresh(**kwargs):
                return {}

    @contextlib.contextmanager
    def scope():
        yield db_session

    def queue(**kwargs):
        reindexes.append(kwargs)
        return ReindexDispatch.QUEUED

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", FakeClient)
    monkeypatch.setattr("app.services.search.reindex_dispatch.queue_transcript_reindex", queue)
    monkeypatch.setattr(
        "app.services.search.indexing_service._invalidate_chat_retrieval_cache", lambda: None
    )
    return {"updates": updates, "reindexes": reindexes}


def _rename(world) -> dict[str, Any]:
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    result: dict[str, Any] = propagate_speaker_rename.apply(
        kwargs={
            "file_uuid": str(world.file.uuid),
            "old_names": ["SPEAKER_01"],
            "new_name": "Dana Renamed",
        }
    ).get()
    return result


def test_a_rename_in_an_off_scope_queues_a_reindex_and_rewrites_nothing(
    db_session, normal_user, seams
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    result = _rename(world)

    assert result["status"] == "reindex_queued"
    assert seams["updates"] == []
    assert seams["reindexes"] == [
        {
            "file_id": world.file.id,
            "file_uuid": str(world.file.uuid),
            "user_id": normal_user.id,
        }
    ]


def test_a_rename_in_an_on_scope_rewrites_in_place(db_session, normal_user, seams):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    result = _rename(world)

    assert result["status"] == "success"
    assert len(seams["updates"]) == 1
    assert seams["reindexes"] == []


def test_a_rename_with_an_unreadable_policy_rebuilds_instead_of_patching(
    db_session, normal_user, seams, monkeypatch
):
    """Fail closed: when the policy cannot be read the file counts as off."""
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    def broken(db, file_id):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(policy, "voiceprints_allowed_for_file", broken)

    result = _rename(world)

    assert result["status"] == "reindex_queued"
    assert seams["updates"] == []


def test_a_rename_whose_reindex_cannot_be_queued_is_reported_as_failed(
    db_session, normal_user, seams, monkeypatch
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )
    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.queue_transcript_reindex",
        lambda **kwargs: ReindexDispatch.FAILED,
    )

    result = _rename(world)

    assert result["status"] == "failed"
    assert seams["updates"] == []
