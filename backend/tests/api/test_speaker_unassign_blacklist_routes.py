"""The HTTP surface of the unassign rule and the blacklist (#1233).

Driven through the real app (routing, auth, the endpoint, the service and
``speaker_cleanup``) against savepoint-isolated rows; OpenSearch is the recorders of
``speaker_os_seams`` / ``speaker_assignment_seams``, so this passes with no cluster
reachable (CI) and against a live one. The live-store proof is
``tests/integration/test_deletion_residue_live.py``.
"""

from __future__ import annotations

import pytest

from app.models.media import Speaker
from app.models.media import SpeakerCannotLink
from app.models.media import SpeakerCluster
from app.models.media import SpeakerProfileBlacklist
from tests.fixtures.speaker_cleanup_seams import VOICE_A
from tests.fixtures.speaker_cleanup_seams import VOICE_B
from tests.fixtures.speaker_cleanup_seams import VOICE_C
from tests.fixtures.speaker_cleanup_seams import cosine
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

MATCH = 0.99999
NAME = "Dana"


def _headers(user_token_headers: dict) -> dict:
    return {k: v for k, v in user_token_headers.items() if not k.startswith("_")}


def _promoted_pair_plus_victim(db, user, seams):
    """Speakers a, b, c linked to ``Dana`` and grouped in its promoted cluster."""
    profile = make_profile(db, user, NAME)
    speakers = []
    for voice in (VOICE_A, VOICE_B, VOICE_C):
        sp = make_speaker(db, user, make_file(db, user))
        sp.profile_id, sp.display_name, sp.verified = profile.id, NAME, True
        seams["voiceprints"][str(sp.uuid)] = voice
        speakers.append(sp)
    cluster = make_cluster(db, user, *speakers, promoted_to=profile.id, label=NAME)
    return profile, cluster, speakers


def _unassign(client, headers, cluster, victim, blacklist=True):
    return client.post(
        f"/api/speaker-clusters/{cluster.uuid}/unassign",
        json={"speaker_uuids": [str(victim.uuid)], "blacklist": blacklist},
        headers=headers,
    )


def _blacklisted(db, speaker_id: int, profile_id: int) -> bool:
    rows: int = (
        db.query(SpeakerProfileBlacklist)
        .filter(
            SpeakerProfileBlacklist.speaker_id == speaker_id,
            SpeakerProfileBlacklist.profile_id == profile_id,
        )
        .count()
    )
    return rows == 1


def test_unassign_route_unlinks_the_profile_and_records_both_blacklists(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    profile, cluster, (a, b, c) = _promoted_pair_plus_victim(
        db_session, normal_user, speaker_os_seams
    )

    resp = _unassign(client, _headers(user_token_headers), cluster, c)

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    assert (c.profile_id, c.display_name, c.cluster_id) == (None, None, None)
    assert _blacklisted(db_session, c.id, profile.id)
    assert (
        db_session.query(SpeakerCannotLink).filter(SpeakerCannotLink.speaker_id == c.id).count()
        == 2
    ), "the speaker-to-speaker half of the blacklist is missing"
    survivors = [x + y for x, y in zip(VOICE_A, VOICE_B, strict=True)]
    assert cosine(speaker_os_seams["profiles"][str(profile.uuid)], survivors) >= MATCH
    assert db_session.get(SpeakerCluster, cluster.id).member_count == 2


def test_unassign_route_without_blacklist_still_unlinks_but_records_nothing(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    """``blacklist: false`` is the user's explicit opt-out of the blacklist, not of the rule."""
    profile, cluster, (_a, _b, c) = _promoted_pair_plus_victim(
        db_session, normal_user, speaker_os_seams
    )

    resp = _unassign(client, _headers(user_token_headers), cluster, c, blacklist=False)

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(Speaker, c.id).profile_id is None
    assert not _blacklisted(db_session, c.id, profile.id)


def test_cluster_merge_route_reports_the_member_it_refused_to_move(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    headers = _headers(user_token_headers)
    profile, cluster, (_a, _b, c) = _promoted_pair_plus_victim(
        db_session, normal_user, speaker_os_seams
    )
    assert _unassign(client, headers, cluster, c).status_code == 200
    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    source = make_cluster(db_session, normal_user, c, label=None)

    resp = client.post(f"/api/speaker-clusters/{source.uuid}/merge/{cluster.uuid}", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["blocked_speaker_uuids"] == [str(c.uuid)]
    db_session.expire_all()
    assert db_session.get(Speaker, c.id).cluster_id == source.id
    assert db_session.get(Speaker, c.id).profile_id is None


def test_merging_a_corrected_speaker_carries_its_blacklist_to_the_target(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    headers = _headers(user_token_headers)
    profile, cluster, (a, b, c) = _promoted_pair_plus_victim(
        db_session, normal_user, speaker_os_seams
    )
    assert _unassign(client, headers, cluster, c).status_code == 200
    db_session.expire_all()
    target = make_speaker(
        db_session, normal_user, db_session.get(Speaker, c.id).media_file, name="SPEAKER_01"
    )
    speaker_os_seams["voiceprints"][str(target.uuid)] = VOICE_C
    db_session.commit()

    resp = client.post(f"/api/speakers/{c.uuid}/merge/{target.uuid}", headers=headers)

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert _blacklisted(db_session, target.id, profile.id), (
        "the merged-away correction died with the source row"
    )
    partners = {
        int(r.cannot_link_speaker_id)
        for r in db_session.query(SpeakerCannotLink).filter(
            SpeakerCannotLink.speaker_id == target.id
        )
    }
    assert partners == {a.id, b.id}


@pytest.mark.parametrize("route", ["assign-profile", "verify-accept"])
def test_naming_one_speaker_explicitly_lifts_its_correction(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, route
):
    headers = _headers(user_token_headers)
    profile, cluster, (a, _b, c) = _promoted_pair_plus_victim(
        db_session, normal_user, speaker_os_seams
    )
    assert _unassign(client, headers, cluster, c).status_code == 200
    assert _blacklisted(db_session, c.id, profile.id), "control: the unassign recorded it"

    if route == "assign-profile":
        resp = client.post(
            f"/api/speaker-profiles/speakers/{c.uuid}/assign-profile",
            params={"profile_uuid": str(profile.uuid)},
            headers=headers,
        )
    else:
        resp = client.post(
            f"/api/speakers/{c.uuid}/verify",
            params={"action": "accept", "profile_uuid": str(profile.uuid)},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(Speaker, c.id).profile_id == profile.id
    assert not _blacklisted(db_session, c.id, profile.id)
    assert (
        db_session.query(SpeakerCannotLink)
        .filter(
            SpeakerCannotLink.speaker_id == c.id, SpeakerCannotLink.cannot_link_speaker_id == a.id
        )
        .count()
        == 0
    )


def test_batch_assign_route_refuses_the_corrected_speaker(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    headers = _headers(user_token_headers)
    profile, cluster, (_a, _b, c) = _promoted_pair_plus_victim(
        db_session, normal_user, speaker_os_seams
    )
    assert _unassign(client, headers, cluster, c).status_code == 200

    resp = client.post(
        "/api/speaker-clusters/batch-verify",
        json={
            "speaker_uuids": [str(c.uuid)],
            "action": "assign",
            "profile_uuid": str(profile.uuid),
        },
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["updated_count"], body["failed_count"]) == (0, 1), body
    db_session.expire_all()
    assert db_session.get(Speaker, c.id).profile_id is None
    assert _blacklisted(db_session, c.id, profile.id)
