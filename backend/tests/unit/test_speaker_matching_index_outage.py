"""Speaker matching when the voiceprint index cannot answer.

Matching asks two kNN questions per speaker (its consolidated profiles, then individual
known speakers) and acts on the answer: a match records a suggestion (or links the
profile), "no match" stores the voiceprint as a NEW voice. Both lookups used to answer an
outage with their empty value, so a blip recorded every speaker as unknown, and nothing
ever asked again. These tests run the real lookups against a stand-in client and check an
outage raises instead, with nothing written for the speaker it hit and nothing committed;
the controls show a cluster that answers "nothing" still takes the no-match path.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.speaker_matching_service import SpeakerMatchingService
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

VOICE = [1.0, 0.0, 0.0, 0.0]
OTHER = [0.0, 1.0, 0.0, 0.0]


def _refused() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "connection refused", Exception("refused"))


class _Indices:
    def exists(self, **_: Any) -> bool:
        return True


class _SpeakerIndex:
    """A speaker index holding no profiles and no known speakers, or one that is down.

    ``down_for`` lists query vectors whose kNN raises; ``down`` makes every search raise.
    """

    def __init__(self) -> None:
        self.indices = _Indices()
        self.down = False
        self.down_for: list[list[float]] = []

    def search(self, **kw: Any) -> dict[str, Any]:
        knn = kw["body"]["query"].get("knn")
        vector = list(knn["embedding"]["vector"]) if knn else None
        if self.down or (vector is not None and vector in self.down_for):
            raise _refused()
        return {"hits": {"total": {"value": 0}, "hits": []}}


@pytest.fixture
def index(monkeypatch) -> _SpeakerIndex:
    """The real profile and speaker kNN, reading a stand-in client."""
    from app.services.opensearch_service import matching

    fake = _SpeakerIndex()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    monkeypatch.setattr("app.services.opensearch_service.client.opensearch_client", fake)
    monkeypatch.setattr(matching, "ensure_indices_exist", lambda: None)
    monkeypatch.setattr(matching, "get_active_speaker_index", lambda: "speakers")
    monkeypatch.setattr(matching, "query_embedding_dimension_mismatch", lambda *_: False)
    return fake


@pytest.fixture
def stored(monkeypatch) -> list[str]:
    """Speaker UUIDs whose voiceprint the no-match path stored as a new voice."""
    sink: list[str] = []

    def _record_stored(**kw: object) -> bool:
        sink.append(str(kw["speaker_uuid"]))
        return True

    monkeypatch.setattr(
        "app.services.speaker_matching.match_handling.add_speaker_embedding", _record_stored
    )
    return sink


@pytest.fixture
def commits(db_session, monkeypatch) -> list[int]:
    """Counts commits; a rollback discards only uncommitted work, as in production."""
    seen: list[int] = []
    real_commit = db_session.commit

    def commit() -> None:
        seen.append(1)
        real_commit()

    def rollback() -> None:
        nested = db_session.get_nested_transaction()
        if nested is not None:
            nested.rollback()

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(db_session, "rollback", rollback)
    return seen


# ---------------------------------------------------------------------------
# match_speaker_to_profiles
# ---------------------------------------------------------------------------


def test_a_profile_knn_outage_is_not_no_match(db_session, normal_user, index):
    org = make_org(db_session)
    index.down = True

    with pytest.raises(OpenSearchUnavailableError):
        SpeakerMatchingService(db_session, None).match_speaker_to_profiles(
            np.array(VOICE), normal_user.id, organization_id=org.id
        )


def test_no_profiles_is_still_no_match(db_session, normal_user, index):
    org = make_org(db_session)

    result = SpeakerMatchingService(db_session, None).match_speaker_to_profiles(
        np.array(VOICE), normal_user.id, organization_id=org.id
    )

    assert result is None


# ---------------------------------------------------------------------------
# process_speaker_embeddings_native / _process_single_speaker
# ---------------------------------------------------------------------------


def _two_speakers(db, user) -> tuple[Any, Any, Any]:
    org = make_org(db)
    media_file = file_in(db, user, org)
    first = make_speaker(db, user, media_file, name="SPEAKER_00")
    second = make_speaker(db, user, media_file, name="SPEAKER_01")
    db.commit()
    return media_file, first, second


def test_an_outage_mid_file_stores_no_voice_for_that_speaker_and_commits_nothing(
    db_session, normal_user, index, stored, commits
):
    media_file, first, second = _two_speakers(db_session, normal_user)
    commits.clear()
    index.down_for.append(OTHER)

    with pytest.raises(OpenSearchUnavailableError):
        SpeakerMatchingService(db_session, None).process_speaker_embeddings_native(
            media_file.id,
            normal_user.id,
            {first.id: np.array(VOICE), second.id: np.array(OTHER)},
        )

    assert stored == [str(first.uuid)], "the unanswered speaker was stored as a new voice"
    assert commits == []


def test_a_file_whose_lookups_all_answer_stores_each_new_voice(
    db_session, normal_user, index, stored, commits
):
    media_file, first, second = _two_speakers(db_session, normal_user)
    commits.clear()

    results = SpeakerMatchingService(db_session, None).process_speaker_embeddings_native(
        media_file.id,
        normal_user.id,
        {first.id: np.array(VOICE), second.id: np.array(OTHER)},
    )

    assert [r["status"] for r in results] == ["new", "new"]
    assert stored == [str(first.uuid), str(second.uuid)]
    assert commits == [1]
