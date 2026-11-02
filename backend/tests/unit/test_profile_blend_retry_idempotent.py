"""A retried matching run must not fold an already-linked speaker into its profile twice.

An auto-accepted profile match blends the speaker's voice into the profile's stored
centroid and commits straight away. If a LATER speaker in the same file then hits a
voiceprint-index outage, the run raises and its task retries the whole file, so the first
speaker is matched and auto-accepted again. These tests run that sequence and compare the
stored centroid with the one a single clean run produces; the control is that clean run.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.speaker_matching_service import SpeakerMatchingService
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

VOICE = [1.0, 0.0, 0.0, 0.0]
OTHER = [0.0, 1.0, 0.0, 0.0]
STORED = [0.0, 0.0, 1.0, 0.0]
#: The stored centroid (one prior voice) blended once with VOICE.
ONE_BLEND = [2**-0.5, 0.0, 2**-0.5, 0.0]


@pytest.fixture
def index(monkeypatch, speaker_os_seams) -> dict[str, Any]:
    """Profile kNN that answers VOICE with the profile; speaker kNN that can be down for OTHER."""
    state: dict[str, Any] = {"profile": None, "down": False}

    def profile_knn(embedding, user_id, **kw):
        if list(embedding) == VOICE:
            p = state["profile"]
            return [
                {
                    "profile_id": p.id,
                    "profile_uuid": str(p.uuid),
                    "profile_name": p.name,
                    "similarity": 0.95,
                    "embedding_count": 1,
                }
            ]
        return []

    def speaker_knn(embedding, user_id, **kw):
        if state["down"] and list(embedding) == OTHER:
            raise OpenSearchUnavailableError("speaker kNN failed: connection refused")
        return None

    monkeypatch.setattr(
        "app.services.profile_embedding_service.ProfileEmbeddingService"
        ".calculate_profile_similarity",
        staticmethod(profile_knn),
    )
    for module in ("match_queries", "match_storage"):
        monkeypatch.setattr(
            f"app.services.speaker_matching.{module}.find_matching_speaker", speaker_knn
        )
    monkeypatch.setattr(
        "app.services.speaker_matching.match_handling.add_speaker_embedding", lambda **kw: True
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.get_profile_embedding",
        lambda profile_uuid: speaker_os_seams["profiles"].get(str(profile_uuid)),
    )
    monkeypatch.setattr("app.services.opensearch_service.update_speaker_profile", lambda **kw: True)
    monkeypatch.setattr(
        SpeakerMatchingService, "_propagate_profile_assignment", lambda self, *a, **k: None
    )
    return state


@pytest.fixture
def production_rollback(db_session, monkeypatch) -> None:
    """``db.rollback()`` discards only uncommitted work, as it does in production."""

    def rollback() -> None:
        nested = db_session.get_nested_transaction()
        if nested is not None:
            nested.rollback()

    monkeypatch.setattr(db_session, "rollback", rollback)


def _world(db, user, index, seams) -> tuple[Any, Any, Any, Any]:
    org = make_org(db)
    profile = make_profile(db, user, name="Dana")
    profile.organization_id = org.id
    earlier = make_speaker(db, user, file_in(db, user, org), name="SPEAKER_00")
    earlier.profile_id = profile.id
    media_file = file_in(db, user, org)
    first = make_speaker(db, user, media_file, name="SPEAKER_00")
    second = make_speaker(db, user, media_file, name="SPEAKER_01")
    db.commit()
    index["profile"] = profile
    seams["profiles"][str(profile.uuid)] = list(STORED)
    return profile, media_file, first, second


def _match(db, user, media_file, first, second) -> list[dict[str, Any]]:
    return SpeakerMatchingService(db, None).process_speaker_embeddings_native(
        media_file.id, user.id, {first.id: np.array(VOICE), second.id: np.array(OTHER)}
    )


@pytest.mark.usefixtures("production_rollback")
def test_a_retry_after_an_outage_blends_the_linked_speaker_once(
    db_session, normal_user, index, speaker_os_seams
):
    profile, media_file, first, second = _world(db_session, normal_user, index, speaker_os_seams)
    index["down"] = True
    with pytest.raises(OpenSearchUnavailableError):
        _match(db_session, normal_user, media_file, first, second)
    index["down"] = False

    _match(db_session, normal_user, media_file, first, second)

    assert speaker_os_seams["profiles"][str(profile.uuid)] == pytest.approx(ONE_BLEND)


def test_a_single_clean_run_blends_the_new_voice_once(
    db_session, normal_user, index, speaker_os_seams
):
    profile, media_file, first, second = _world(db_session, normal_user, index, speaker_os_seams)

    results = _match(db_session, normal_user, media_file, first, second)

    assert [r["status"] for r in results] == ["matched", "new"]
    assert speaker_os_seams["profiles"][str(profile.uuid)] == pytest.approx(ONE_BLEND)
