"""An opted-out file's voice never survives inside a profile or cluster average (issue #1356).

The averages read voiceprint documents from the index, so deleting a file's documents already
keeps them out. These tests prove the SECOND line of defence: with the opted-out file's document
still READABLE (an opt-out whose purge has not run yet, or failed), the average excludes it.
The control in each test is the same world with the file not declined, where the voice IS in
the average, so "exclude everything" cannot pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast

import pytest

from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.services.privacy import purge
from app.services.privacy.policy import clear_write_backstop_cache
from app.services.profile_embedding_service import ProfileEmbeddingService
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


@pytest.fixture
def world(db_session, normal_user, speaker_os_seams, monkeypatch):
    db = db_session
    file_a, file_b = make_file(db, normal_user), make_file(db, normal_user)
    profile = make_profile(db, normal_user, "Pat")
    sa = make_speaker(db, normal_user, file_a, profile_id=profile.id, display_name="Pat")
    sb = make_speaker(db, normal_user, file_b, profile_id=profile.id, display_name="Pat")
    db.commit()
    speaker_os_seams["voiceprints"].update({str(sa.uuid): VOICE_A, str(sb.uuid): VOICE_B})
    # Every voiceprint stays READABLE: only the consent column can keep A out.
    monkeypatch.setattr(
        "app.services.profile_embedding_service.get_speaker_embedding",
        lambda speaker_uuid: speaker_os_seams["voiceprints"].get(str(speaker_uuid)),
    )
    clear_write_backstop_cache()
    return {"file_a": file_a, "file_b": file_b, "profile": profile, "sa": sa, "sb": sb}


def _stored(seams, profile):
    return seams["profiles"][str(profile.uuid)]


def test_control_without_an_opt_out_both_voices_are_in_the_profile(
    db_session, world, speaker_os_seams
):
    assert ProfileEmbeddingService.update_profile_embedding(db_session, world["profile"].id)

    stored = _stored(speaker_os_seams, world["profile"])
    assert cosine(stored, VOICE_A) > 0.3
    assert cosine(stored, VOICE_B) > 0.3


def test_profile_average_excludes_a_declined_file_even_with_its_document_readable(
    db_session, world, speaker_os_seams
):
    world["file_a"].voiceprint_consent = "declined"
    db_session.commit()

    assert ProfileEmbeddingService.update_profile_embedding(db_session, world["profile"].id)

    stored = _stored(speaker_os_seams, world["profile"])
    assert cosine(stored, VOICE_B) == pytest.approx(1.0)
    assert cosine(stored, VOICE_A) == pytest.approx(0.0, abs=1e-9)


def test_a_profile_whose_only_voices_are_declined_is_cleared_not_kept(
    db_session, world, speaker_os_seams
):
    for key in ("file_a", "file_b"):
        world[key].voiceprint_consent = "declined"
    db_session.commit()

    with pytest.raises(Exception, match="no members"):
        ProfileEmbeddingService.update_profile_embedding(
            db_session, world["profile"].id, strict=True
        )

    assert str(world["profile"].uuid) not in speaker_os_seams["profiles"]


def test_cluster_centroid_excludes_a_declined_file(
    db_session, normal_user, world, speaker_os_seams, monkeypatch
):
    from app.services.speaker_clustering_service import SpeakerClusteringService

    cluster = SpeakerCluster(user_id=normal_user.id, label="c", member_count=2)
    db_session.add(cluster)
    db_session.flush()
    for key in ("sa", "sb"):
        db_session.add(SpeakerClusterMember(cluster_id=cluster.id, speaker_id=world[key].id))
    world["file_a"].voiceprint_consent = "declined"
    db_session.commit()
    stored: dict = {}
    service = SpeakerClusteringService(db_session)
    monkeypatch.setattr(
        service,
        "_get_speaker_embedding",
        lambda speaker: speaker_os_seams["voiceprints"].get(str(speaker.uuid)),
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.store_cluster_embedding",
        lambda **kw: stored.update(kw) or True,
    )

    service._update_cluster_centroid(cluster, normal_user.id, strict=True)

    assert cosine(stored["embedding"], VOICE_B) == pytest.approx(1.0)
    assert cosine(stored["embedding"], VOICE_A) == pytest.approx(0.0, abs=1e-9)


def test_opt_out_purge_of_a_file_leaves_the_profile_as_the_other_file_only(
    db_session, normal_user, world, speaker_os_seams, monkeypatch
):
    index = FakeOpenSearch()
    index.add(str(world["sa"].uuid), normal_user.id, None)
    index.add(str(world["sb"].uuid), normal_user.id, None)
    index.add(f"profile_{world['profile'].uuid}", normal_user.id, None, document_type="profile")
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", index)
    world["file_a"].voiceprint_consent = "declined"
    db_session.commit()
    scope = purge.PurgeScope(kind="files", file_ids=(world["file_a"].id,))

    assert (
        purge.targets_still_enabled(
            db_session, scope, [purge.VOICEPRINTS], purge_reason=purge.OPT_OUT
        )
        == []
    )
    result = purge.run_purge_pass(
        scope,
        [purge.VOICEPRINTS],
        session_factory=lambda: cast("Session", _KeepOpen(db_session)),
        reindex=lambda files: 0,
    )

    survivors = index.ids()
    assert str(world["sa"].uuid) not in survivors
    assert str(world["sb"].uuid) in survivors
    assert cosine(_stored(speaker_os_seams, world["profile"]), VOICE_B) == pytest.approx(1.0)
    assert result.residual().ok, result.residual().counts


def test_opt_out_is_a_known_reason_and_an_unknown_one_still_raises(db_session):
    scope = purge.PurgeScope(kind="files", file_ids=(1,))

    assert purge.OPT_OUT in purge.PURGE_REASONS
    with pytest.raises(ValueError, match="unknown purge reason"):
        purge.targets_still_enabled(db_session, scope, [purge.VOICEPRINTS], purge_reason="bogus")
