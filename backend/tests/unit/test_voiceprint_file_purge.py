"""A ``files`` purge removes those files' voiceprints and recomputes, never deletes, profiles.

Real Postgres rows; the speaker index is the in-memory ``FakeOpenSearch`` and the profile and
cluster writes go through ``speaker_os_seams``. The voiceprint read seam answers only for a
document still in the fake index, so a recompute that ran before the delete (or read a deleted
voice) would be caught by the asserted vector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast

import pytest

from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services.privacy import purge
from app.services.privacy.policy import clear_write_backstop_cache
from tests.fixtures.fake_speaker_index import FakeOpenSearch
from tests.fixtures.speaker_cleanup_seams import VOICE_A
from tests.fixtures.speaker_cleanup_seams import VOICE_B
from tests.fixtures.speaker_cleanup_seams import cosine
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class _KeepOpen:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


class _StuckIndex(FakeOpenSearch):
    """An index whose deletes report success and remove nothing."""

    def delete_by_query(self, index, body, refresh=True, conflicts="proceed"):
        self.queries.append(body)
        return {"deleted": 0, "failures": []}


def _wire(monkeypatch, seams, index):
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", index)

    def get(speaker_uuid):
        if str(speaker_uuid) not in index.ids():
            return None
        return seams["voiceprints"].get(str(speaker_uuid))

    monkeypatch.setattr("app.services.opensearch_service.get_speaker_embedding", get)
    monkeypatch.setattr("app.services.profile_embedding_service.get_speaker_embedding", get)
    clear_write_backstop_cache()


@pytest.fixture
def world(db_session, normal_user, speaker_os_seams):
    db = db_session
    file_a, file_b = make_file(db, normal_user), make_file(db, normal_user)
    profile = make_profile(db, normal_user, "Pat")
    lonely = make_profile(db, normal_user, "Lone")
    sa = make_speaker(db, normal_user, file_a, profile_id=profile.id, display_name="Pat")
    sb = make_speaker(db, normal_user, file_b, profile_id=profile.id, display_name="Pat")
    only_a = make_speaker(db, normal_user, file_a, "SPEAKER_01", profile_id=lonely.id)
    db.commit()
    speaker_os_seams["voiceprints"].update(
        {str(sa.uuid): VOICE_A, str(sb.uuid): VOICE_B, str(only_a.uuid): VOICE_A}
    )
    return {
        "file_a": file_a,
        "file_b": file_b,
        "profile": profile,
        "lonely": lonely,
        "sa": sa,
        "sb": sb,
        "only_a": only_a,
    }


def _index_with(index, normal_user, world):
    for key in ("sa", "sb", "only_a"):
        index.add(str(world[key].uuid), normal_user.id, None)
    for key in ("profile", "lonely"):
        index.add(f"profile_{world[key].uuid}", normal_user.id, None, document_type="profile")
    return index


def _run(db_session, scope):
    return purge.run_purge_pass(
        scope,
        [purge.VOICEPRINTS],
        session_factory=lambda: cast("Session", _KeepOpen(db_session)),
        reindex=lambda files: 0,
    )


def test_file_purge_removes_its_voiceprints_and_recomputes_the_profile(
    db_session, normal_user, world, speaker_os_seams, monkeypatch
):
    index = _index_with(FakeOpenSearch(), normal_user, world)
    _wire(monkeypatch, speaker_os_seams, index)

    result = _run(db_session, purge.PurgeScope(kind="files", file_ids=(world["file_a"].id,)))

    survivors = index.ids()
    assert str(world["sa"].uuid) not in survivors
    assert str(world["only_a"].uuid) not in survivors
    assert str(world["sb"].uuid) in survivors  # file B is untouched
    # The profile rows stay; neither profile document was deleted by the purge itself.
    assert db_session.query(SpeakerProfile).filter_by(id=world["profile"].id).count() == 1
    assert db_session.query(SpeakerProfile).filter_by(id=world["lonely"].id).count() == 1
    assert f"profile_{world['profile'].uuid}" in survivors
    # P is recomputed from B alone: exactly B's voice, not an average with A's.
    stored = speaker_os_seams["profiles"][str(world["profile"].uuid)]
    assert cosine(stored, VOICE_B) == pytest.approx(1.0)
    assert cosine(stored, VOICE_A) == pytest.approx(0.0, abs=1e-9)
    # A profile left with no voiceprint has its embedding erased, the row kept.
    assert str(world["lonely"].uuid) in speaker_os_seams["erased_profiles"]
    assert str(world["lonely"].uuid) not in speaker_os_seams["profiles"]
    # The person's links stay (the voiceprint goes, not the identity).
    db_session.refresh(world["sa"])
    assert world["sa"].profile_id == world["profile"].id
    counts = result.counts()
    assert counts["opensearch"]["speaker_docs_deleted"] == 2
    assert counts["opensearch"]["profile_docs_deleted"] == 0
    assert counts["opensearch"]["profiles_recomputed"] == 2
    assert result.residual().ok, result.residual().counts
    assert result.errors == []


def test_the_residual_check_fails_when_a_voiceprint_remains(
    db_session, normal_user, world, speaker_os_seams, monkeypatch
):
    index = _index_with(_StuckIndex(), normal_user, world)
    _wire(monkeypatch, speaker_os_seams, index)

    result = _run(db_session, purge.PurgeScope(kind="files", file_ids=(world["file_a"].id,)))

    residual = result.residual()
    assert not residual.ok
    assert residual.counts["speaker_docs"] == 2
    # Not confirmed gone, so the profile is NOT recomputed from them: its embedding is erased.
    assert str(world["profile"].uuid) not in speaker_os_seams["profiles"]
    assert str(world["profile"].uuid) in speaker_os_seams["erased_profiles"]


def test_a_file_purge_leaves_other_files_speakers_alone(
    db_session, normal_user, world, speaker_os_seams, monkeypatch
):
    index = _index_with(FakeOpenSearch(), normal_user, world)
    _wire(monkeypatch, speaker_os_seams, index)
    world["sb"].suggested_name = "Pat"
    world["sb"].suggestion_source = "voice_match"
    db_session.commit()

    _run(db_session, purge.PurgeScope(kind="files", file_ids=(world["file_a"].id,)))

    sb = db_session.query(Speaker).filter_by(id=world["sb"].id).one()
    assert sb.suggestion_source == "voice_match"  # another file's voice suggestion survives
