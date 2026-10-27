"""The read side of the privacy policy: suggestions, clusters and recompute routes (#1280).

Switching voiceprint matching off HIDES what already exists and never deletes it (plan D5), and
a request that would create or recompute a voiceprint answers 409 ``voiceprints_disabled``
(plan D8). Every "off" case here has an "on" control beside it: a guard that always hid
everything would otherwise pass.

The switch is written straight into ``system_settings`` (the instance layer) and the endpoints
read it back through the real policy, so removing the guard from an endpoint turns the
matching test red by ASSERTION (a suggestion is still there), not by an import error. The
OpenSearch reads are recorded rather than served, so "no kNN was run" is asserted directly
instead of inferred from an empty answer.

``db_session`` is shared with the app under test, so the rows a request redacts in memory are
the very objects the test holds: ``expire_all`` before the next request stands in for the
fresh session a real second request gets.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest
from fastapi import status

from app.core import constants
from app.models.media import Speaker
from app.models.system_settings import SystemSettings
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

SPEAKERS = "/api/speakers"
CLUSTERS = "/api/speaker-clusters"
PROFILES = "/api/speaker-profiles"

OFF = {"voiceprints": "false"}


def _switch_voiceprints(db, value: str) -> None:
    """Set the instance-layer voiceprint switch ('true' / 'false')."""
    row = (
        db.query(SystemSettings)
        .filter(SystemSettings.key == constants.PRIVACY_VOICEPRINT_MATCHING_KEY)
        .first()
    )
    if row is None:
        db.add(SystemSettings(key=constants.PRIVACY_VOICEPRINT_MATCHING_KEY, value=value))
    else:
        row.value = value
    db.commit()
    db.expire_all()


@pytest.fixture
def knn(monkeypatch) -> dict[str, int]:
    """Serve the batch suggestion reads from recorders and count how often they ran."""
    calls = {"embeddings": 0, "msearch": 0}

    def embeddings(uuids):
        calls["embeddings"] += 1
        return {str(u): [1.0, 0.0, 0.0, 0.0] for u in uuids}

    def msearch(*, speaker_embeddings, user_id, threshold, organization_id):
        calls["msearch"] += 1
        return {
            u: [
                {
                    "profile_id": 5,
                    "profile_name": "Alice Profile",
                    "speaker_count": 3,
                    "similarity": 0.9,
                }
            ]
            for u in speaker_embeddings
        }

    monkeypatch.setattr("app.services.opensearch_service.get_speaker_embeddings_batch", embeddings)
    monkeypatch.setattr("app.services.opensearch_service.msearch_profile_knn_batch", msearch)
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", object())
    monkeypatch.setattr(
        "app.services.smart_speaker_suggestion_service._check_opensearch_profiles_exist",
        lambda *a, **k: True,
    )
    return calls


@pytest.fixture
def world(db_session, normal_user) -> dict[str, Any]:
    """One file with a voiceprint-suggested speaker and an LLM-suggested one."""
    file = make_file(db_session, normal_user)
    voice = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_00",
        suggested_name="Voice Alice",
        confidence=0.9,
        suggestion_source="voice_match",
        verified=False,
    )
    llm = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_01",
        suggested_name="Llm Bob",
        confidence=0.8,
        suggestion_source="llm_analysis",
        verified=False,
    )
    db_session.commit()
    return {"file": file, "voice": voice, "llm": llm}


def _by_uuid(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {i["uuid"]: i for i in items}


# ---------------------------------------------------------------------------
# GET /speakers: suggestions
# ---------------------------------------------------------------------------


def test_the_speaker_list_offers_voice_suggestions_while_voiceprints_are_on(
    client, user_token_headers, world, knn
):
    resp = client.get(
        SPEAKERS, headers=user_token_headers, params={"file_uuid": world["file"].uuid}
    )
    assert resp.status_code == status.HTTP_200_OK
    voice = _by_uuid(resp.json())[str(world["voice"].uuid)]
    assert voice["suggested_name"] == "Voice Alice"
    assert voice["suggestion_source"] == "voice_match"
    assert [s["name"] for s in voice["profile_suggestions"]] == ["Alice Profile"]
    assert knn == {"embeddings": 1, "msearch": 1}


def test_the_speaker_list_hides_voice_suggestions_and_runs_no_knn_while_off(
    client, user_token_headers, world, knn, db_session
):
    _switch_voiceprints(db_session, "false")

    resp = client.get(
        SPEAKERS, headers=user_token_headers, params={"file_uuid": world["file"].uuid}
    )

    assert resp.status_code == status.HTTP_200_OK
    voice = _by_uuid(resp.json())[str(world["voice"].uuid)]
    assert voice["suggested_name"] is None
    assert voice["suggestion_source"] is None
    assert voice["confidence"] is None
    assert voice["profile_suggestions"] == []
    # Not merely an empty answer: nothing was read from the voiceprint index at all.
    assert knn == {"embeddings": 0, "msearch": 0}


def test_an_llm_suggestion_is_transcript_derived_and_stays_while_voiceprints_are_off(
    client, user_token_headers, world, knn, db_session
):
    _switch_voiceprints(db_session, "false")

    resp = client.get(
        SPEAKERS, headers=user_token_headers, params={"file_uuid": world["file"].uuid}
    )

    llm = _by_uuid(resp.json())[str(world["llm"].uuid)]
    assert llm["suggested_name"] == "Llm Bob"
    assert llm["suggestion_source"] == "llm_analysis"


def test_a_verified_speaker_keeps_its_voice_sourced_name_while_voiceprints_are_off(
    client, user_token_headers, world, knn, db_session
):
    """A person verified it: that is a user assertion, not biometric data."""
    world["voice"].verified = True
    db_session.commit()
    _switch_voiceprints(db_session, "false")

    resp = client.get(
        SPEAKERS, headers=user_token_headers, params={"file_uuid": world["file"].uuid}
    )

    voice = _by_uuid(resp.json())[str(world["voice"].uuid)]
    assert voice["suggested_name"] == "Voice Alice"


def test_switching_off_hides_and_switching_on_restores_the_stored_suggestion(
    client, user_token_headers, world, knn, db_session
):
    """Ignore, not delete: the database row never changes, and re-enabling brings it back."""
    params = {"file_uuid": world["file"].uuid}
    _switch_voiceprints(db_session, "false")
    hidden = client.get(SPEAKERS, headers=user_token_headers, params=params)
    assert _by_uuid(hidden.json())[str(world["voice"].uuid)]["suggested_name"] is None

    db_session.expire_all()
    stored = db_session.get(Speaker, world["voice"].id)
    assert stored.suggested_name == "Voice Alice"
    assert stored.suggestion_source == "voice_match"
    assert stored.confidence == pytest.approx(0.9)

    _switch_voiceprints(db_session, "true")
    shown = client.get(SPEAKERS, headers=user_token_headers, params=params)
    assert _by_uuid(shown.json())[str(world["voice"].uuid)]["suggested_name"] == "Voice Alice"


def test_the_single_speaker_read_hides_a_voice_suggestion_while_off(
    client, user_token_headers, world, db_session
):
    url = f"{SPEAKERS}/{world['voice'].uuid}"
    assert client.get(url, headers=user_token_headers).json()["suggested_name"] == "Voice Alice"

    _switch_voiceprints(db_session, "false")
    body = client.get(url, headers=user_token_headers).json()

    assert body["suggested_name"] is None
    assert body["suggestion_source"] is None
    assert body["confidence"] is None


def test_a_users_own_switch_hides_their_suggestions_without_touching_the_instance(
    client, user_token_headers, world, knn, db_session, normal_user
):
    from app.models.prompt import UserSetting

    db_session.add(
        UserSetting(
            user_id=normal_user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db_session.commit()

    resp = client.get(
        SPEAKERS, headers=user_token_headers, params={"file_uuid": world["file"].uuid}
    )

    voice = _by_uuid(resp.json())[str(world["voice"].uuid)]
    assert voice["suggested_name"] is None
    assert knn["msearch"] == 0


# ---------------------------------------------------------------------------
# GET /speaker-profiles/speakers/{uuid}/suggestions
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_knn(monkeypatch) -> dict[str, int]:
    calls = {"similarity": 0}

    def similarity(embedding, user_id, **kwargs):
        calls["similarity"] += 1
        return [
            {
                "profile_id": 5,
                "profile_uuid": str(uuid_pkg.uuid4()),
                "profile_name": "Alice Profile",
                "similarity": 0.9,
                "embedding_count": 3,
                "last_update": None,
            }
        ]

    monkeypatch.setattr(
        "app.services.profile_embedding_service.ProfileEmbeddingService.calculate_profile_similarity",
        staticmethod(similarity),
    )
    return calls


def test_the_profile_suggestions_endpoint_runs_the_knn_while_on(
    client, user_token_headers, world, profile_knn, speaker_os_seams
):
    speaker_os_seams["voiceprints"][str(world["voice"].uuid)] = [1.0, 0.0, 0.0, 0.0]

    resp = client.get(
        f"{PROFILES}/speakers/{world['voice'].uuid}/suggestions", headers=user_token_headers
    )

    assert resp.status_code == status.HTTP_200_OK
    assert [s["profile_name"] for s in resp.json() if s["source"] == "voice_embedding"] == [
        "Alice Profile"
    ]
    assert profile_knn["similarity"] == 1


def test_the_profile_suggestions_endpoint_runs_no_knn_while_off(
    client, user_token_headers, world, profile_knn, speaker_os_seams, db_session
):
    speaker_os_seams["voiceprints"][str(world["voice"].uuid)] = [1.0, 0.0, 0.0, 0.0]
    _switch_voiceprints(db_session, "false")

    resp = client.get(
        f"{PROFILES}/speakers/{world['voice'].uuid}/suggestions", headers=user_token_headers
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == []
    assert profile_knn["similarity"] == 0


# ---------------------------------------------------------------------------
# /speaker-clusters: lists are empty, recompute routes answer 409
# ---------------------------------------------------------------------------


@pytest.fixture
def two_member_cluster(db_session, normal_user):
    file_a, file_b = make_file(db_session, normal_user), make_file(db_session, normal_user)
    a = make_speaker(db_session, normal_user, file_a)
    b = make_speaker(db_session, normal_user, file_b)
    return make_cluster(db_session, normal_user, a, b)


def test_the_cluster_list_is_empty_while_off_and_full_while_on(
    client, user_token_headers, two_member_cluster, db_session
):
    on = client.get(CLUSTERS, headers=user_token_headers)
    assert str(two_member_cluster.uuid) in {c["uuid"] for c in on.json()["items"]}

    _switch_voiceprints(db_session, "false")
    off = client.get(CLUSTERS, headers=user_token_headers)

    assert off.status_code == status.HTTP_200_OK
    assert off.json()["items"] == []
    assert off.json()["total"] == 0


def test_the_cluster_stats_count_no_clusters_while_off(
    client, user_token_headers, two_member_cluster, db_session
):
    on = client.get(f"{CLUSTERS}/stats", headers=user_token_headers).json()
    assert (on["total_clusters"], on["clustered_speakers"]) == (1, 2)

    _switch_voiceprints(db_session, "false")
    off = client.get(f"{CLUSTERS}/stats", headers=user_token_headers).json()

    assert (off["total_clusters"], off["clustered_speakers"]) == (0, 0)
    assert off["total_speakers"] == on["total_speakers"]


def test_the_cluster_detail_answers_like_a_missing_cluster_while_off(
    client, user_token_headers, two_member_cluster, db_session
):
    url = f"{CLUSTERS}/{two_member_cluster.uuid}"
    assert client.get(url, headers=user_token_headers).status_code == status.HTTP_200_OK

    _switch_voiceprints(db_session, "false")

    assert client.get(url, headers=user_token_headers).status_code == status.HTTP_404_NOT_FOUND


def test_the_inbox_drops_the_voice_suggestion_and_the_cluster_while_off(
    client, user_token_headers, two_member_cluster, world, db_session
):
    on = client.get(f"{CLUSTERS}/unverified/inbox", headers=user_token_headers).json()["items"]
    voice_on = next(i for i in on if i["speaker_uuid"] == str(world["voice"].uuid))
    assert voice_on["suggested_name"] == "Voice Alice"

    _switch_voiceprints(db_session, "false")
    off = client.get(f"{CLUSTERS}/unverified/inbox", headers=user_token_headers).json()["items"]

    voice_off = next(i for i in off if i["speaker_uuid"] == str(world["voice"].uuid))
    assert voice_off["suggested_name"] is None
    assert voice_off["suggestion_source"] is None
    assert all(i["cluster_uuid"] is None for i in off)


def test_recluster_answers_409_with_the_locking_layer_while_off(
    client, user_token_headers, db_session
):
    ok = client.post(f"{CLUSTERS}/recluster", headers=user_token_headers)
    assert ok.status_code == status.HTTP_200_OK
    assert ok.json()["status"] == "started"

    _switch_voiceprints(db_session, "false")
    refused = client.post(f"{CLUSTERS}/recluster", headers=user_token_headers)

    assert refused.status_code == status.HTTP_409_CONFLICT
    assert refused.json()["detail"] == {"code": "voiceprints_disabled", "locked_by": "instance"}


@pytest.mark.parametrize(
    ("method", "path", "control_status"),
    [
        ("post", "{c}/analyze-outliers", status.HTTP_404_NOT_FOUND),
        ("post", "{c}/promote", status.HTTP_400_BAD_REQUEST),
        ("post", "{c}/split", status.HTTP_400_BAD_REQUEST),
        ("post", "{c}/merge/{d}", status.HTTP_400_BAD_REQUEST),
    ],
)
def test_the_recompute_routes_answer_409_while_off(
    client, user_token_headers, db_session, method, path, control_status
):
    """Control first: with the layer on the same call reaches the route's own not-found."""
    body: dict[str, Any] | None = {"name": "n"} if path.endswith("promote") else None
    if path.endswith("split"):
        body = {"speaker_uuids": [str(uuid_pkg.uuid4())]}
    url = f"{CLUSTERS}/" + path.format(c=uuid_pkg.uuid4(), d=uuid_pkg.uuid4())

    control = getattr(client, method)(url, headers=user_token_headers, json=body)
    assert control.status_code == control_status

    _switch_voiceprints(db_session, "false")
    refused = getattr(client, method)(url, headers=user_token_headers, json=body)

    assert refused.status_code == status.HTTP_409_CONFLICT
    assert refused.json()["detail"]["code"] == "voiceprints_disabled"
