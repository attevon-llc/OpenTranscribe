"""The chunk index carries the speaker label and profile link the policy allows (#1280).

``index_transcript_search_task`` and ``reindex_task._extract_file_metadata`` write a speaker
label and a ``profile_id`` into every chunk document. While a file's voiceprints are off the
label of a voiceprint-made identity is the diarizer's, and neither a ``voiceprint`` nor a
``voiceprint_link`` identity carries its profile link. A typed name and a person's link are
never touched. Each case has an "on" control. The policy decision is asked once per file, not
once per segment.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from app.services.privacy import policy
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import BOB
from tests.fixtures.provenance_world import CAROL
from tests.fixtures.provenance_world import build_world


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def indexed(monkeypatch, db_session):
    """Run ``index_transcript_search_task`` for a file; return the segments it would index."""
    from app.services.search import indexing_service
    from app.tasks import search_indexing_task

    captured: dict[str, Any] = {}

    def fake_chunks(self, **kwargs):
        captured.update(kwargs)
        return {"chunk_count": len(kwargs["segments"])}

    monkeypatch.setattr(
        indexing_service.TranscriptIndexingService, "index_transcript_chunks", fake_chunks
    )
    monkeypatch.setattr("app.services.opensearch_service.index_transcript", lambda *a, **k: None)

    @contextlib.contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)

    def run(world, user) -> list[dict[str, Any]]:
        result = search_indexing_task.index_transcript_search_task.apply(
            kwargs={
                "file_id": world.file.id,
                "file_uuid": str(world.file.uuid),
                "user_id": user.id,
            }
        ).get()
        assert result["status"] == "success", result
        return list(captured["segments"])

    return run


def _by_speaker(segments: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {s["speaker_id"]: s for s in segments}


def test_an_off_file_is_indexed_with_diarizer_labels_and_no_machine_links(
    db_session, normal_user, indexed
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    by_speaker = _by_speaker(indexed(world, normal_user))

    assert by_speaker[world.alice.id]["speaker"] == "SPEAKER_00"
    assert by_speaker[world.alice.id]["profile_id"] is None
    assert by_speaker[world.carol.id]["speaker"] == CAROL  # a typed name stays
    assert by_speaker[world.carol.id]["profile_id"] is None  # only the machine link goes
    assert by_speaker[world.bob.id]["speaker"] == BOB
    assert by_speaker[world.bob.id]["profile_id"] == world.profiles["bob"].id


def test_an_on_file_is_indexed_with_every_name_and_link(db_session, normal_user, indexed):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    by_speaker = _by_speaker(indexed(world, normal_user))

    assert by_speaker[world.alice.id]["speaker"] == ALICE
    assert by_speaker[world.alice.id]["profile_id"] == world.profiles["alice"].id
    assert by_speaker[world.carol.id]["speaker"] == CAROL
    assert by_speaker[world.carol.id]["profile_id"] == world.profiles["carol"].id
    assert by_speaker[world.bob.id]["profile_id"] == world.profiles["bob"].id


def test_reindex_metadata_applies_the_same_rule(db_session, normal_user):
    from app.tasks.reindex_task import _extract_file_metadata

    off = build_world(db_session, normal_user, organization=make_org(db_session, voiceprints=False))
    on = build_world(db_session, normal_user, organization=make_org(db_session, voiceprints=True))

    off_meta = _extract_file_metadata(db_session, off.file)
    on_meta = _extract_file_metadata(db_session, on.file)
    assert off_meta is not None and on_meta is not None
    off_segments = _by_speaker(off_meta["segments"])
    on_segments = _by_speaker(on_meta["segments"])

    assert off_segments[off.alice.id]["speaker"] == "SPEAKER_00"
    assert "profile_id" not in off_segments[off.alice.id]
    assert "profile_id" not in off_segments[off.carol.id]
    assert off_segments[off.carol.id]["speaker"] == CAROL
    assert off_segments[off.bob.id]["profile_id"] == off.profiles["bob"].id
    assert on_segments[on.alice.id]["speaker"] == ALICE
    assert on_segments[on.alice.id]["profile_id"] == on.profiles["alice"].id
    assert on_segments[on.carol.id]["profile_id"] == on.profiles["carol"].id


def test_the_policy_is_asked_once_per_file_not_once_per_segment(
    db_session, normal_user, indexed, monkeypatch
):
    world = build_world(
        db_session,
        normal_user,
        organization=make_org(db_session, voiceprints=False),
        segments_per_speaker=40,
    )
    asked: list[Any] = []
    real = policy.voiceprint_decision

    def counting(db, **kwargs):
        asked.append(kwargs.get("media_file_id"))
        return real(db, **kwargs)

    monkeypatch.setattr(policy, "voiceprint_decision", counting)

    segments = indexed(world, normal_user)

    assert len(segments) == 120
    assert 1 <= len(asked) <= 2  # the file's decision; never one per segment


def test_an_unreadable_policy_hides_the_voiceprint_label(
    db_session, normal_user, indexed, monkeypatch
):
    """Fail closed: when the decision cannot be made the diarizer label is what is indexed."""
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    def broken(db, file_id):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(policy, "voiceprints_allowed_for_file", broken)

    by_speaker = _by_speaker(indexed(world, normal_user))

    assert by_speaker[world.alice.id]["speaker"] == "SPEAKER_00"
    assert by_speaker[world.alice.id]["profile_id"] is None
    assert by_speaker[world.bob.id]["speaker"] == BOB
