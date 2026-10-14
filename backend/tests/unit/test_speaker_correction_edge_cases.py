"""Edge cases of a user's speaker corrections that the #1233 sweep left open.

Each test is one concrete scenario the owner's rules decide (issue #1233, decisions 1-5):

* a bulk action honours a correction and REPORTS what it skipped; one explicit action on
  one speaker is the only thing that lifts it, and only when it says so;
* an automatic path never overwrites an identity a speaker already has;
* a speaker moved to another profile is no longer in the old profile's cluster;
* rejecting a profile identification is a correction like an unassignment.

Real Postgres rows; OpenSearch answered by the recorders in ``speaker_os_seams`` /
``speaker_assignment_seams`` with real cosine math, so this passes with no cluster
reachable and against a live one.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from app.models.media import Speaker
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfileBlacklist
from tests.fixtures.speaker_cleanup_seams import cosine
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

MATCH = 0.99999
BOB = "Bob"
ALICE = "Alice"


def _unit(v: list[float]) -> list[float]:
    a = np.array(v, dtype=np.float64)
    unit: list[float] = (a / np.linalg.norm(a)).tolist()
    return unit


# Bob's voice (three takes, pairwise cosine > 0.9) and Alice's, which is far from it.
VA = _unit([1.0, 0.2, 0.0, 0.0])
VB = _unit([1.0, 0.0, 0.2, 0.0])
VC = _unit([1.0, 0.0, 0.0, 0.3])
VX = _unit([0.0, 0.0, 0.0, 1.0])


def _mean(*voices: list[float]) -> list[float]:
    mean: list[float] = np.mean([np.array(v) for v in voices], axis=0).tolist()
    return mean


def _service(db):
    from app.services.speaker_clustering_service import SpeakerClusteringService

    return SpeakerClusteringService(db)


def _voiced(db, user, seams, voice, *, profile=None, display=None) -> Any:
    sp = make_speaker(db, user, make_file(db, user))
    seams["voiceprints"][str(sp.uuid)] = voice
    if profile is not None:
        sp.profile_id, sp.verified = profile.id, True
        sp.display_name = display if display is not None else profile.name
    elif display is not None:
        sp.display_name, sp.verified = display, True
    db.flush()
    return sp


def _bob_cluster(db, user, seams, c_voice=VC) -> dict[str, Any]:
    """A, B, C linked to ``Bob`` and grouped in Bob's promoted cluster."""
    bob = make_profile(db, user, BOB)
    a = _voiced(db, user, seams, VA, profile=bob)
    b = _voiced(db, user, seams, VB, profile=bob)
    c = _voiced(db, user, seams, c_voice, profile=bob)
    cluster = make_cluster(db, user, a, b, c, promoted_to=bob.id, label=BOB)
    seams["clusters"][str(cluster.uuid)] = _mean(VA, VB, c_voice)
    seams["profiles"][str(bob.uuid)] = _mean(VA, VB, c_voice)
    return {"profile": bob, "a": a, "b": b, "c": c, "cluster": cluster}


def _unassign(db, user, group, key="c") -> None:
    _service(db).unassign_speakers(
        str(group["cluster"].uuid), [str(group[key].uuid)], user.id, blacklist=True
    )
    db.expire_all()


def _blacklisted(db, speaker_id: int, profile_id: int) -> bool:
    count: int = (
        db.query(SpeakerProfileBlacklist)
        .filter(
            SpeakerProfileBlacklist.speaker_id == speaker_id,
            SpeakerProfileBlacklist.profile_id == profile_id,
        )
        .count()
    )
    return count == 1


def _member(db, cluster_id: int, speaker_id: int) -> bool:
    count: int = (
        db.query(SpeakerClusterMember)
        .filter(
            SpeakerClusterMember.cluster_id == cluster_id,
            SpeakerClusterMember.speaker_id == speaker_id,
        )
        .count()
    )
    return count == 1


# ------------------------------------------------- decision 2: bulk honours, single overrides


def test_batch_name_refuses_a_name_the_speaker_was_unassigned_from(
    db_session, normal_user, speaker_os_seams
):
    """A bulk "name" with the corrected name is refused per speaker and reported."""
    group = _bob_cluster(db_session, normal_user, speaker_os_seams)
    _unassign(db_session, normal_user, group)
    d = _voiced(db_session, normal_user, speaker_os_seams, VC)
    db_session.commit()

    result = _service(db_session).batch_verify_speakers(
        [str(group["c"].uuid), str(d.uuid)], normal_user.id, action="name", display_name="bob"
    )

    assert (result["updated_count"], result["failed_count"]) == (1, 1), result
    assert result["blocked_speaker_uuids"] == [str(group["c"].uuid)], result
    db_session.expire_all()
    assert db_session.get(Speaker, group["c"].id).display_name is None
    assert db_session.get(Speaker, d.id).display_name == "bob", "control: d was named"


@pytest.mark.parametrize("action", ["assign", "accept", "name"])
def test_one_explicit_override_on_one_speaker_lifts_the_correction(
    db_session, normal_user, speaker_os_seams, action
):
    """The Inbox's per-speaker action: deliberate, one speaker, and it says so."""
    group = _bob_cluster(db_session, normal_user, speaker_os_seams)
    bob = group["profile"]
    _unassign(db_session, normal_user, group)
    c = db_session.get(Speaker, group["c"].id)
    if action == "accept":
        c.suggested_name, c.confidence = BOB, 0.6
    db_session.commit()
    kwargs: dict[str, Any] = {
        "assign": {"profile_uuid": str(bob.uuid)},
        "accept": {},
        "name": {"display_name": BOB},
    }[action]

    refused = _service(db_session).batch_verify_speakers(
        [str(c.uuid)], normal_user.id, action=action, **kwargs
    )
    assert refused["blocked_speaker_uuids"] == [str(c.uuid)], "control: refused without override"
    assert _blacklisted(db_session, c.id, bob.id)

    result = _service(db_session).batch_verify_speakers(
        [str(c.uuid)], normal_user.id, action=action, override_unassignment=True, **kwargs
    )

    assert (result["updated_count"], result["failed_count"]) == (1, 0), result
    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    assert c.display_name == BOB
    assert not _blacklisted(db_session, c.id, bob.id), "the explicit override lifted nothing"
    if action == "assign":
        assert c.profile_id == bob.id


def test_an_override_is_refused_for_more_than_one_speaker(
    db_session, normal_user, speaker_os_seams
):
    """Lifting a correction is a per-speaker decision; a batch never carries one."""
    group = _bob_cluster(db_session, normal_user, speaker_os_seams)
    _unassign(db_session, normal_user, group)

    with pytest.raises(ValueError, match="one speaker"):
        _service(db_session).batch_verify_speakers(
            [str(group["c"].uuid), str(group["a"].uuid)],
            normal_user.id,
            action="assign",
            profile_uuid=str(group["profile"].uuid),
            override_unassignment=True,
        )

    db_session.expire_all()
    assert _blacklisted(db_session, group["c"].id, group["profile"].id)


def test_batch_assign_rewrites_the_assigned_speakers_own_documents(
    db_session, normal_user, speaker_os_seams
):
    """An assignment completes: the voiceprint document carries the new profile too."""
    bob = make_profile(db_session, normal_user, BOB)
    d = _voiced(db_session, normal_user, speaker_os_seams, VA)
    db_session.commit()

    _service(db_session).batch_verify_speakers(
        [str(d.uuid)], normal_user.id, action="assign", profile_uuid=str(bob.uuid)
    )

    assert speaker_os_seams["doc_profiles"].get(str(d.uuid)) == bob.id, (
        "the speaker document still names no profile, so a profile kNN suggestion "
        "reads the pre-assignment identity"
    )


# ---------------------------------- an automatic path never overwrites an existing identity


@pytest.mark.parametrize("identity", ["linked", "typed"])
def test_clustering_never_takes_over_a_speaker_that_already_has_an_identity(
    db_session, normal_user, speaker_os_seams, identity
):
    """Bob's cluster sounds like Alice's speaker; clustering must not rename her Bob.

    ``linked``: Alice's profile link (typed name). ``typed``: a name accepted or typed
    without a profile (Inbox "accept", batch "name"). Either way the speaker has no
    cluster, which every Re-cluster All leaves a profile of one speaker in.
    """
    group = _bob_cluster(db_session, normal_user, speaker_os_seams)
    alice = make_profile(db_session, normal_user, ALICE) if identity == "linked" else None
    s = _voiced(db_session, normal_user, speaker_os_seams, VC, profile=alice, display=ALICE)
    d = _voiced(db_session, normal_user, speaker_os_seams, VC)  # control: no identity
    db_session.commit()

    # The control first: it must find Bob's cluster, not the singleton s would leave.
    for sp in (d, s):
        _service(db_session).cluster_speakers_for_file(sp.media_file_id, normal_user.id)

    db_session.expire_all()
    s = db_session.get(Speaker, s.id)
    assert s.display_name == ALICE, "clustering overwrote a name the user gave this speaker"
    assert s.profile_id == (alice.id if alice else None)
    assert s.cluster_id != group["cluster"].id, "Alice's voice joined Bob's cluster"
    d = db_session.get(Speaker, d.id)
    assert (d.cluster_id, d.profile_id, d.display_name) == (
        group["cluster"].id,
        group["profile"].id,
        BOB,
    ), "control: an unnamed look-alike joins and takes Bob"


# ------------------------------- decision 5: moved to another profile = out of the old cluster


def test_naming_a_cluster_member_someone_else_takes_it_out_of_the_old_cluster(
    db_session, normal_user, speaker_os_seams
):
    """Typing "Alice" on a member of Bob's cluster: the section is Alice's, not Bob's."""
    from app.api.endpoints.speaker_update import auto_create_or_assign_profile

    group = _bob_cluster(db_session, normal_user, speaker_os_seams, c_voice=VX)
    c = db_session.get(Speaker, group["c"].id)
    c.display_name = ALICE

    assert auto_create_or_assign_profile(c, ALICE, db_session) is True
    db_session.commit()

    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    cluster_id = group["cluster"].id
    assert c.profile_id is not None and c.profile_id != group["profile"].id
    assert c.cluster_id != cluster_id and not _member(db_session, cluster_id, c.id), (
        "Alice is still a member of Bob's cluster"
    )
    assert cosine(speaker_os_seams["clusters"][str(group["cluster"].uuid)], _mean(VA, VB)) >= (
        MATCH
    ), "Bob's centroid still averages Alice's voice, so new Alice speakers match Bob"
    for key in ("a", "b"):
        assert _member(db_session, cluster_id, group[key].id), "control: Bob's members stay"


def test_batch_assigning_a_member_to_another_profile_takes_it_out_of_the_old_cluster(
    db_session, normal_user, speaker_os_seams
):
    group = _bob_cluster(db_session, normal_user, speaker_os_seams, c_voice=VX)
    alice = make_profile(db_session, normal_user, ALICE)
    db_session.commit()

    _service(db_session).batch_verify_speakers(
        [str(group["c"].uuid)], normal_user.id, action="assign", profile_uuid=str(alice.uuid)
    )

    db_session.expire_all()
    assert not _member(db_session, group["cluster"].id, group["c"].id)
    assert cosine(speaker_os_seams["clusters"][str(group["cluster"].uuid)], _mean(VA, VB)) >= (
        MATCH
    )


def test_merging_leaves_a_member_with_another_identity_behind_and_reports_it(
    db_session, normal_user, speaker_os_seams
):
    """Merging a group into Bob's cluster never drags a speaker the user called Alice."""
    group = _bob_cluster(db_session, normal_user, speaker_os_seams)
    alice = make_profile(db_session, normal_user, ALICE)
    s = _voiced(db_session, normal_user, speaker_os_seams, VX, profile=alice)
    e = _voiced(db_session, normal_user, speaker_os_seams, VC)
    source = make_cluster(db_session, normal_user, s, e, label=None)
    service = _service(db_session)

    merged = service.merge_clusters(str(source.uuid), str(group["cluster"].uuid), normal_user.id)

    assert merged is not None
    assert service.blocked_speaker_uuids == [str(s.uuid)]
    db_session.expire_all()
    s = db_session.get(Speaker, s.id)
    assert (s.cluster_id, s.profile_id, s.display_name) == (source.id, alice.id, ALICE)
    assert db_session.get(Speaker, e.id).cluster_id == group["cluster"].id, "control: e moved"


def test_a_moved_segment_leaves_the_old_speakers_voice_and_both_keep_their_tenant(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """Moving a segment off Bob's speaker: its audio is no longer in Bob's voiceprint.

    The target gains the segment's embedding by a weighted add; the source loses it by
    the inverse, so Bob's profile stops averaging audio the user said is someone else's.
    Both documents are rewritten whole, so both must keep the org file's tenant.
    """
    import contextlib
    import uuid as uuid_pkg

    from app.models.media import TranscriptSegment
    from app.models.organization import Organization
    from app.tasks import speaker_embedding_task as seb

    org = Organization(name=f"spk-review-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    media = make_file(db_session, normal_user)
    media.organization_id = org.id
    bob = make_profile(db_session, normal_user, BOB)
    bob.organization_id = org.id
    source = make_speaker(db_session, normal_user, media, name="SPEAKER_00")
    source.profile_id, source.display_name, source.verified = bob.id, BOB, True
    target = make_speaker(db_session, normal_user, media, name="SPEAKER_01")
    speaker_os_seams["voiceprints"][str(source.uuid)] = VA
    speaker_os_seams["segment_counts"][str(source.uuid)] = 2
    speaker_os_seams["voiceprints"][str(target.uuid)] = VB
    seg = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media.id,
        speaker_id=target.id,  # already moved by the endpoint
        start_time=0.0,
        end_time=2.0,
        text="hello",
    )
    db_session.add(seg)
    db_session.commit()

    @contextlib.contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(seb, "session_scope", scope)
    monkeypatch.setattr(seb, "_extract_segment_embedding", lambda plan: np.array(VX))

    result = seb.update_speaker_embedding_on_reassignment.apply(
        args=[str(seg.uuid), "unused", str(target.uuid), str(source.uuid), normal_user.id]
    ).get()

    assert result["status"] == "success", result
    without = _unit((2 * np.array(VA) - np.array(VX)).tolist())
    assert cosine(speaker_os_seams["voiceprints"][str(source.uuid)], without) >= MATCH, (
        "the moved segment's voice is still inside the speaker it was moved off"
    )
    assert speaker_os_seams["segment_counts"][str(source.uuid)] == 1
    assert cosine(speaker_os_seams["profiles"][str(bob.uuid)], without) >= MATCH, (
        "Bob's profile still averages the audio the user moved to someone else"
    )
    assert speaker_os_seams["doc_orgs"] == {str(source.uuid): org.id, str(target.uuid): org.id}, (
        "a rewritten voiceprint lost its org stamp and now reads as a personal speaker"
    )


def test_a_voice_match_suggestion_skips_a_name_and_a_speaker_the_user_ruled_out(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """The no-profile-match branch: a >=0.75 voice-match suggestion moves the label."""
    from app.services.speaker_cleanup import record_unassignment
    from app.services.speaker_matching_service import SpeakerMatchingService

    group = _bob_cluster(db_session, normal_user, speaker_os_seams)
    by_name = _voiced(db_session, normal_user, speaker_os_seams, VC)
    by_pair = _voiced(db_session, normal_user, speaker_os_seams, VC)
    control = _voiced(db_session, normal_user, speaker_os_seams, VC)
    record_unassignment(
        db_session, [by_name.id], against_speaker_ids=[], profile_id=group["profile"].id
    )
    record_unassignment(
        db_session, [by_pair.id], against_speaker_ids=[group["a"].id], profile_id=None
    )
    db_session.commit()
    svc = SpeakerMatchingService(db_session, None)
    a = group["a"]
    monkeypatch.setattr(
        svc,
        "find_and_store_speaker_matches",
        lambda *args, **kw: [{"speaker_id": a.id, "display_name": "bob", "confidence": 0.9}],
    )
    monkeypatch.setattr(
        "app.services.speaker_matching_service.add_speaker_embedding", lambda **kw: True
    )

    for sp in (by_name, by_pair, control):
        svc._handle_no_speaker_match(sp, np.array(VC), normal_user.id, sp.media_file_id)
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(Speaker, by_name.id).suggested_name is None
    assert db_session.get(Speaker, by_pair.id).suggested_name is None
    assert db_session.get(Speaker, control.id).suggested_name == "bob", "control"


# ------------------------------------------- rejecting an identification is a correction too


def test_rejecting_a_profile_identification_is_honoured_by_retroactive_matching(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """``verify?action=reject`` on a speaker auto-linked to Bob: Bob must not come back."""
    from app.api.endpoints import speaker_update
    from app.api.endpoints.speakers import _reject_speaker_suggestion

    group = _bob_cluster(db_session, normal_user, speaker_os_seams)
    bob = group["profile"]
    c = db_session.get(Speaker, group["c"].id)
    d = _voiced(db_session, normal_user, speaker_os_seams, VC)  # control
    db_session.commit()

    _reject_speaker_suggestion(c, int(c.id), db_session)
    db_session.expire_all()

    monkeypatch.setattr(speaker_update, "_send_bulk_update_notification", lambda *a: None)
    speaker_update.trigger_retroactive_matching(db_session.get(Speaker, group["a"].id), db_session)

    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    assert (c.profile_id, c.display_name, c.suggested_name) == (None, None, None), (
        "retroactive matching re-applied the identity the user rejected"
    )
    d = db_session.get(Speaker, d.id)
    assert (d.profile_id, d.display_name) == (bob.id, BOB), "control: d was matched"
    assert _blacklisted(db_session, c.id, bob.id), "the rejection recorded no correction"


def test_rejecting_a_suggestion_blocks_the_profile_it_named(
    db_session, normal_user, speaker_os_seams
):
    """An unlinked speaker carrying a "Bob" suggestion: rejecting it means not Bob."""
    from app.api.endpoints.speakers import _reject_speaker_suggestion

    bob = make_profile(db_session, normal_user, BOB)
    s = _voiced(db_session, normal_user, speaker_os_seams, VA)
    s.suggested_name, s.confidence = "bob", 0.8
    db_session.commit()

    _reject_speaker_suggestion(s, int(s.id), db_session)

    db_session.expire_all()
    assert _blacklisted(db_session, s.id, bob.id)
