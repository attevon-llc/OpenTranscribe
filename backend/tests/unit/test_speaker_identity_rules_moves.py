"""Characterization of the unassign rule and the identity moves split out of speaker_cleanup.

``speaker_identity_rules`` (who a speaker may not be linked to, and recording a correction)
and ``speaker_identity_moves`` (unlink / join / detach) decide which voice may carry which
name, and they feed the erasure path through ``detach_from_cluster``'s returned impact. These
tests pin the branches the wider suites only reach incidentally, against real Postgres rows.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.speaker import SpeakerCannotLink
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerProfileBlacklist
from app.services import speaker_identity_moves as moves
from app.services import speaker_identity_rules as rules
from app.services.speaker_rename_tracker import SpeakerRenameTracker
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker


def _world(db, user, n=3):
    f = make_file(db, user)
    return f, [make_speaker(db, user, f, name=f"SPEAKER_{i:02d}") for i in range(n)]


# ------------------------------------------------------------------------------- rules


def test_cannot_link_is_symmetric_even_for_a_one_directional_row(db_session, normal_user):
    _, (a, b, c) = _world(db_session, normal_user)
    db_session.add(SpeakerCannotLink(speaker_id=a.id, cannot_link_speaker_id=b.id, reason="x"))
    db_session.flush()

    out = rules.cannot_link_partners(db_session, [a.id, b.id, c.id])

    assert out == {a.id: {b.id}, b.id: {a.id}, c.id: set()}
    assert rules.cannot_link_partners(db_session, []) == {}


def test_blocked_profile_names_unions_blacklist_and_partners_profile_casefolded(
    db_session, normal_user
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    alice = make_profile(db_session, normal_user, "ALICE")
    b.profile_id = alice.id
    db_session.add(SpeakerProfileBlacklist(speaker_id=a.id, profile_id=bob.id, reason="x"))
    db_session.add(SpeakerCannotLink(speaker_id=a.id, cannot_link_speaker_id=b.id, reason="x"))
    db_session.flush()

    out = rules.blocked_profile_names(db_session, [a.id, c.id])

    assert out == {a.id: {"bob", "alice"}, c.id: set()}
    assert rules.blocked_profile_names(db_session, []) == {}


def test_speaker_blocked_from_profile_by_blacklist_by_partner_link_and_not_otherwise(
    db_session, normal_user
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    carol = make_profile(db_session, normal_user, "Carol")
    b.profile_id = carol.id
    db_session.add(SpeakerProfileBlacklist(speaker_id=a.id, profile_id=bob.id, reason="x"))
    db_session.add(SpeakerCannotLink(speaker_id=a.id, cannot_link_speaker_id=b.id, reason="x"))
    db_session.flush()

    assert rules.speaker_blocked_from_profile(db_session, a.id, bob.id) is True
    assert rules.speaker_blocked_from_profile(db_session, a.id, carol.id) is True
    assert rules.speaker_blocked_from_profile(db_session, a.id, None) is False
    assert rules.speaker_blocked_from_profile(db_session, c.id, bob.id) is False
    other = make_profile(db_session, normal_user, "Dan")
    assert rules.speaker_blocked_from_profile(db_session, a.id, other.id) is False


def test_speaker_blocked_from_cluster_by_promoted_profile_by_member_partner_and_not_otherwise(
    db_session, normal_user
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    plain = make_cluster(db_session, normal_user, b)
    promoted = make_cluster(db_session, normal_user, c, promoted_to=bob.id, label="p")
    db_session.add(SpeakerCannotLink(speaker_id=a.id, cannot_link_speaker_id=b.id, reason="x"))
    db_session.add(SpeakerProfileBlacklist(speaker_id=a.id, profile_id=bob.id, reason="x"))
    db_session.commit()

    assert rules.speaker_blocked_from_cluster(db_session, a.id, plain) is True
    assert rules.speaker_blocked_from_cluster(db_session, a.id, promoted) is True
    assert rules.speaker_blocked_from_cluster(db_session, c.id, plain) is False


def test_has_other_identity_cases(db_session, normal_user):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    carol = make_profile(db_session, normal_user, "Carol")

    a.profile_id = carol.id
    assert rules.has_other_identity(db_session, a, bob.id) is True
    a.profile_id = bob.id
    assert rules.has_other_identity(db_session, a, bob.id) is False

    assert rules.has_other_identity(db_session, b, bob.id) is False  # placeholder name
    b.display_name = "  bob "
    assert rules.has_other_identity(db_session, b, bob.id) is False  # same name, casefolded
    b.display_name = "Zed"
    assert rules.has_other_identity(db_session, b, bob.id) is True  # typed someone else
    assert rules.has_other_identity(db_session, b, 10**9) is True  # profile row gone

    cl = make_cluster(db_session, normal_user, c)  # not promoted: never a conflict
    assert rules.identity_conflicts_with_cluster(db_session, b, cl) is False
    cl.promoted_to_profile_id = bob.id
    assert rules.identity_conflicts_with_cluster(db_session, b, cl) is True


def test_blocking_profile_ids_named_matches_only_the_named_blocking_profiles(
    db_session, normal_user
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    carol = make_profile(db_session, normal_user, "Carol")
    b.profile_id = carol.id
    db_session.add(SpeakerProfileBlacklist(speaker_id=a.id, profile_id=bob.id, reason="x"))
    db_session.add(SpeakerCannotLink(speaker_id=a.id, cannot_link_speaker_id=b.id, reason="x"))
    db_session.flush()

    assert rules.blocking_profile_ids_named(db_session, a.id, " BOB ") == {bob.id}
    assert rules.blocking_profile_ids_named(db_session, a.id, "carol") == {carol.id}
    assert rules.blocking_profile_ids_named(db_session, a.id, "nobody") == set()
    assert rules.blocking_profile_ids_named(db_session, c.id, "bob") == set()


def test_record_unassignment_writes_both_directions_once_and_stamps_the_reason(
    db_session, normal_user
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")

    for _ in range(2):  # idempotent
        rules.record_unassignment(
            db_session, [a.id], against_speaker_ids=[b.id, a.id], profile_id=bob.id
        )

    pairs = {
        (r.speaker_id, r.cannot_link_speaker_id, r.reason)
        for r in db_session.query(SpeakerCannotLink).all()
    }
    assert pairs == {
        (a.id, b.id, rules.REASON_UNASSIGNED),
        (b.id, a.id, rules.REASON_UNASSIGNED),
    }
    rows = db_session.query(SpeakerProfileBlacklist).all()
    assert [(r.speaker_id, r.profile_id, r.reason) for r in rows] == [
        (a.id, bob.id, rules.REASON_UNASSIGNED)
    ]

    rules.record_unassignment(db_session, [], against_speaker_ids=[c.id], profile_id=bob.id)
    assert db_session.query(SpeakerCannotLink).count() == 2


def test_lift_profile_blacklist_drops_only_that_speakers_rows_for_that_profile(
    db_session, normal_user
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    other = make_profile(db_session, normal_user, "Other")
    b.profile_id = bob.id
    for pid in (bob.id, other.id):
        db_session.add(SpeakerProfileBlacklist(speaker_id=a.id, profile_id=pid, reason="x"))
    db_session.add(SpeakerProfileBlacklist(speaker_id=c.id, profile_id=bob.id, reason="x"))
    db_session.add(SpeakerCannotLink(speaker_id=a.id, cannot_link_speaker_id=b.id, reason="x"))
    db_session.add(SpeakerCannotLink(speaker_id=b.id, cannot_link_speaker_id=a.id, reason="x"))
    db_session.add(SpeakerCannotLink(speaker_id=a.id, cannot_link_speaker_id=c.id, reason="x"))
    db_session.flush()

    rules.lift_profile_blacklist(db_session, a.id, bob.id)

    left = {(r.speaker_id, r.profile_id) for r in db_session.query(SpeakerProfileBlacklist).all()}
    assert left == {(a.id, other.id), (c.id, bob.id)}
    links = {
        (r.speaker_id, r.cannot_link_speaker_id) for r in db_session.query(SpeakerCannotLink).all()
    }
    assert links == {(a.id, c.id)}


def test_carry_blacklist_copies_corrections_but_not_the_targets_own_profile(
    db_session, normal_user
):
    _, (src, tgt, other) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    carol = make_profile(db_session, normal_user, "Carol")
    tgt.profile_id = bob.id
    for pid in (bob.id, carol.id):
        db_session.add(SpeakerProfileBlacklist(speaker_id=src.id, profile_id=pid, reason="x"))
    db_session.add(
        SpeakerCannotLink(speaker_id=src.id, cannot_link_speaker_id=other.id, reason="x")
    )
    db_session.add(SpeakerCannotLink(speaker_id=src.id, cannot_link_speaker_id=tgt.id, reason="x"))
    db_session.flush()

    rules.carry_blacklist(db_session, src.id, tgt.id)

    carried = {
        (r.profile_id, r.reason)
        for r in db_session.query(SpeakerProfileBlacklist).filter_by(speaker_id=tgt.id)
    }
    assert carried == {(carol.id, rules.REASON_MERGED)}
    merged = {
        (r.speaker_id, r.cannot_link_speaker_id)
        for r in db_session.query(SpeakerCannotLink).filter_by(reason=rules.REASON_MERGED)
    }
    assert merged == {(tgt.id, other.id), (other.id, tgt.id)}  # never linked to itself


# ------------------------------------------------------------------------------- moves


def test_unlink_from_profile_clears_only_profile_derived_names_and_other_profiles_stay(
    db_session, normal_user
):
    f, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    carol = make_profile(db_session, normal_user, "Carol")
    a.profile_id, a.verified, a.display_name, a.suggested_name = bob.id, True, "Bob", "Bob"
    b.profile_id, b.verified, b.display_name, b.suggested_name = bob.id, True, "Typed", "Other"
    c.profile_id, c.display_name = carol.id, "Carol"
    tracker = SpeakerRenameTracker()

    unlinked = moves.unlink_from_profile(db_session, [a, b, c], bob.id, tracker)

    assert unlinked == {a.id, b.id}
    assert (a.profile_id, a.verified, a.display_name, a.suggested_name) == (None, False, None, None)
    assert (b.profile_id, b.verified, b.display_name, b.suggested_name) == (
        None,
        False,
        "Typed",
        "Other",
    )
    assert (c.profile_id, c.display_name) == (carol.id, "Carol")
    # only a label that actually changed is told to the chunk plane: b kept its typed name
    assert tracker.pending == [(f.id, "Bob", "SPEAKER_00")]

    ghost = SpeakerRenameTracker()
    assert moves.unlink_from_profile(db_session, [c], 10**9, ghost) == set()
    assert c.profile_id == carol.id and ghost.pending == []


def test_join_cluster_identity_links_names_and_tracks_for_a_human(db_session, normal_user):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    old = make_profile(db_session, normal_user, "Old")
    a.profile_id = old.id
    cl = make_cluster(db_session, normal_user, c, promoted_to=bob.id)
    tracker = SpeakerRenameTracker()

    touched = moves.join_cluster_identity(
        db_session, a, cl, tracker, source="human", user_id=normal_user.id
    )

    assert touched == {old.id, bob.id}
    assert (a.profile_id, a.display_name, a.verified) == (bob.id, "Bob", True)
    assert a.identity_source == "human"
    assert len(tracker.pending) == 1


def test_join_cluster_identity_refuses_when_unpromoted_blocked_or_profile_gone(
    db_session, normal_user
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    tracker = SpeakerRenameTracker()
    unpromoted = make_cluster(db_session, normal_user, c)
    assert moves.join_cluster_identity(db_session, a, unpromoted, tracker, source="human") == set()

    promoted = make_cluster(db_session, normal_user, b, promoted_to=bob.id, label="p")
    db_session.add(SpeakerProfileBlacklist(speaker_id=a.id, profile_id=bob.id, reason="x"))
    db_session.flush()
    assert moves.join_cluster_identity(db_session, a, promoted, tracker, source="human") == set()

    promoted.promoted_to_profile_id = 10**9
    assert moves.join_cluster_identity(db_session, a, promoted, tracker, source="human") == set()
    assert (a.profile_id, a.display_name) == (None, None)
    assert tracker.pending == []


@pytest.mark.parametrize("blacklist", [True, False])
def test_detach_from_cluster_removes_membership_and_records_only_when_asked(
    db_session, normal_user, blacklist
):
    _, (a, b, c) = _world(db_session, normal_user)
    bob = make_profile(db_session, normal_user, "Bob")
    a.profile_id, a.display_name = bob.id, "Bob"
    cl = make_cluster(db_session, normal_user, a, b, c, promoted_to=bob.id)
    tracker = SpeakerRenameTracker()

    impact = moves.detach_from_cluster(db_session, cl, [a], blacklist=blacklist, tracker=tracker)
    db_session.flush()

    assert cl.member_count == 2
    assert a.cluster_id is None and a.profile_id is None and a.display_name is None
    assert {
        m.speaker_id for m in db_session.query(SpeakerClusterMember).filter_by(cluster_id=cl.id)
    } == {
        b.id,
        c.id,
    }
    assert impact.owner_ids == {normal_user.id}
    assert impact.cluster_uuids == {str(cl.uuid)}
    assert impact.profile_ids == {bob.id}
    assert impact.relabel_speaker_ids == {a.id}
    assert (db_session.query(SpeakerProfileBlacklist).count() == 1) is blacklist
    assert (db_session.query(SpeakerCannotLink).count() == 4) is blacklist


def test_detach_from_cluster_without_a_profile_touches_no_profile(db_session, normal_user):
    _, (a, b, _c) = _world(db_session, normal_user)
    cl = make_cluster(db_session, normal_user, a, b)

    impact = moves.detach_from_cluster(
        db_session, cl, [a], blacklist=True, tracker=SpeakerRenameTracker()
    )
    db_session.flush()

    assert impact.profile_ids == set() and impact.relabel_speaker_ids == set()
    assert db_session.query(SpeakerProfileBlacklist).count() == 0
    assert db_session.query(SpeakerCannotLink).count() == 2
    assert uuid_pkg.UUID(next(iter(impact.cluster_uuids))) == cl.uuid
