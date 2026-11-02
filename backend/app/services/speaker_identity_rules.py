"""The unassign rule and the blacklist every assignment path honours (issue #1233).

Split from ``speaker_cleanup`` by concern: this module is the read-and-record side of the
rule (who a speaker may not be linked to, and recording a correction); the writes that
change identity are in :mod:`app.services.speaker_identity_moves`, and the reconciliation
of the stores after a change is in :mod:`app.services.speaker_cleanup`.

* :func:`speaker_blocked_from_cluster` / :func:`speaker_blocked_from_profile` — the check
  every automatic assignment path makes before it links a speaker.
* :func:`lift_profile_blacklist` — a user who explicitly names one speaker as that
  profile again overrides their own earlier correction; only explicit single-speaker
  actions call it.
* :func:`record_unassignment` — the blacklist and cannot-link rows an unassignment writes.
* :func:`has_other_identity` / :func:`identity_conflicts_with_cluster` — an automatic
  path never renames a speaker that is already somebody else.

A correction outlives a re-run of its file through ``speaker_correction_carryover``.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models.speaker import Speaker
from app.models.speaker import SpeakerCannotLink
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerProfile
from app.models.speaker import SpeakerProfileBlacklist

#: ``reason`` written on the constraint rows a user's unassignment records.
REASON_UNASSIGNED = "unassigned_by_user"
#: ``reason`` written on the rows a speaker merge copies from the source to the target.
REASON_MERGED = "inherited_by_merge"
#: ``reason`` written when a user rejects a profile identification of a speaker.
REASON_REJECTED = "rejected_by_user"


def blacklisted_profile_ids(db: Session, speaker_ids: Iterable[int]) -> dict[int, set[int]]:
    """For each speaker, the profiles it was unassigned from (``speaker_profile_blacklist``)."""

    ids = sorted({int(i) for i in speaker_ids})
    out: dict[int, set[int]] = {i: set() for i in ids}
    if not ids:
        return out
    rows = (
        db.query(SpeakerProfileBlacklist.speaker_id, SpeakerProfileBlacklist.profile_id)
        .filter(SpeakerProfileBlacklist.speaker_id.in_(ids))
        .all()
    )
    for speaker_id, profile_id in rows:
        out[int(speaker_id)].add(int(profile_id))
    return out


def cannot_link_partners(db: Session, speaker_ids: Iterable[int]) -> dict[int, set[int]]:
    """For each speaker, every speaker it must never share a cluster with.

    Symmetric whichever direction a row was written in: unassign writes both, but a
    one-directional row (older data, a hand edit) must still keep the pair apart.
    """
    from sqlalchemy import or_

    ids = sorted({int(i) for i in speaker_ids})
    out: dict[int, set[int]] = {i: set() for i in ids}
    if not ids:
        return out
    rows = (
        db.query(SpeakerCannotLink.speaker_id, SpeakerCannotLink.cannot_link_speaker_id)
        .filter(
            or_(
                SpeakerCannotLink.speaker_id.in_(ids),
                SpeakerCannotLink.cannot_link_speaker_id.in_(ids),
            )
        )
        .all()
    )
    for a, b in rows:
        if int(a) in out:
            out[int(a)].add(int(b))
        if int(b) in out:
            out[int(b)].add(int(a))
    return out


def blocked_profile_names(db: Session, speaker_ids: Iterable[int]) -> dict[int, set[str]]:
    """For each speaker, the casefolded names of every profile it is blocked from.

    For the paths that assign by NAME rather than by profile id (accepting a suggestion,
    a voice-match suggestion copied from another speaker's label): the profiles it was
    unassigned from, and the profiles its cannot-link partners are linked to.
    """
    ids = sorted({int(i) for i in speaker_ids})
    out: dict[int, set[str]] = {i: set() for i in ids}
    if not ids:
        return out
    blocked = blacklisted_profile_ids(db, ids)
    partners = cannot_link_partners(db, ids)
    partner_ids = sorted(set().union(*partners.values()))
    partner_profile: dict[int, int] = {}
    if partner_ids:
        partner_profile = {
            int(sid): int(pid)
            for sid, pid in db.query(Speaker.id, Speaker.profile_id)
            .filter(Speaker.id.in_(partner_ids), Speaker.profile_id.isnot(None))
            .all()
            if pid is not None
        }
    wanted = set().union(*blocked.values()) | set(partner_profile.values())
    names: dict[int, str] = {}
    if wanted:
        names = {
            int(pid): str(name)
            for pid, name in db.query(SpeakerProfile.id, SpeakerProfile.name)
            .filter(SpeakerProfile.id.in_(sorted(wanted)))
            .all()
        }
    for sid in ids:
        profile_ids = blocked[sid] | {
            partner_profile[p] for p in partners[sid] if p in partner_profile
        }
        out[sid] = {names[p].casefold() for p in profile_ids if p in names}
    return out


def speaker_blocked_from_profile(db: Session, speaker_id: int, profile_id: int | None) -> bool:
    """Whether a correction forbids linking this speaker to this profile.

    Blocked when the speaker was unassigned from the profile, or when it is cannot-link
    with a speaker currently linked to it: "not the same person as Y" while Y is Bob
    means "not Bob".
    """
    if profile_id is None:
        return False

    sid = int(speaker_id)
    if int(profile_id) in blacklisted_profile_ids(db, [sid])[sid]:
        return True
    partners = cannot_link_partners(db, [sid])[sid]
    if not partners:
        return False
    linked = (
        db.query(Speaker.id)
        .filter(Speaker.id.in_(sorted(partners)), Speaker.profile_id == int(profile_id))
        .first()
    )
    return linked is not None


def speaker_blocked_from_cluster(db: Session, speaker_id: int, cluster: SpeakerCluster) -> bool:
    """Whether a correction forbids this speaker from joining this cluster.

    Blocked by the promoted profile (:func:`speaker_blocked_from_profile`) or by a
    cannot-link with any current member, read from both membership representations.
    Queries the rows rather than ``cluster.members``, which a caller that has just
    flushed new members may hold stale.
    """
    from sqlalchemy import or_

    if cluster.promoted_to_profile_id is not None and speaker_blocked_from_profile(
        db, speaker_id, int(cluster.promoted_to_profile_id)
    ):
        return True
    partners = cannot_link_partners(db, [int(speaker_id)])[int(speaker_id)]
    if not partners:
        return False
    member_of = (
        db.query(SpeakerClusterMember.speaker_id)
        .filter(SpeakerClusterMember.cluster_id == cluster.id)
        .scalar_subquery()
    )
    conflict = (
        db.query(Speaker.id)
        .filter(
            Speaker.id.in_(sorted(partners)),
            or_(Speaker.id.in_(member_of), Speaker.cluster_id == cluster.id),
        )
        .first()
    )
    return conflict is not None


def has_other_identity(db: Session, speaker: Speaker, profile_id: int) -> bool:
    """Whether the speaker is already somebody other than this profile.

    Linked to a different profile, or carrying a name (typed, or an accepted suggestion)
    that is not this profile's. A voice similarity never outranks an identity the speaker
    already has, so no automatic path takes such a speaker over (issue #1233).
    """
    from app.utils.speaker_labels import is_assigned_speaker_name

    if speaker.profile_id is not None:
        return int(speaker.profile_id) != int(profile_id)
    if not is_assigned_speaker_name(speaker.display_name):
        return False
    name = db.query(SpeakerProfile.name).filter(SpeakerProfile.id == int(profile_id)).scalar()
    return name is None or str(speaker.display_name).strip().casefold() != str(name).casefold()


def identity_conflicts_with_cluster(db: Session, speaker: Speaker, cluster: SpeakerCluster) -> bool:
    """Whether joining this promoted cluster would rename a speaker that is someone else."""
    profile_id = cluster.promoted_to_profile_id
    return profile_id is not None and has_other_identity(db, speaker, int(profile_id))


def blocking_profile_ids_named(db: Session, speaker_id: int, name: str) -> set[int]:
    """The profiles called ``name`` that a correction blocks this speaker from.

    The id-level twin of :func:`blocked_profile_names`, for an explicit override that
    must lift exactly what blocked the name it is applying.
    """
    sid = int(speaker_id)
    candidates = set(blacklisted_profile_ids(db, [sid])[sid])
    partners = cannot_link_partners(db, [sid])[sid]
    if partners:
        candidates |= {
            int(pid)
            for (pid,) in db.query(Speaker.profile_id)
            .filter(Speaker.id.in_(sorted(partners)), Speaker.profile_id.isnot(None))
            .all()
            if pid is not None
        }
    if not candidates:
        return set()
    wanted = name.strip().casefold()
    return {
        int(pid)
        for pid, pname in db.query(SpeakerProfile.id, SpeakerProfile.name)
        .filter(SpeakerProfile.id.in_(sorted(candidates)))
        .all()
        if str(pname).casefold() == wanted
    }


def record_unassignment(
    db: Session,
    speaker_ids: Iterable[int],
    *,
    against_speaker_ids: Iterable[int],
    profile_id: int | None,
    reason: str = REASON_UNASSIGNED,
) -> None:
    """Record that these speakers are neither the others nor the profile. Idempotent.

    Cannot-link rows in both directions against every speaker in ``against_speaker_ids``,
    and a profile blacklist row when ``profile_id`` is set. Adds to the session; the
    caller commits with its own change.
    """
    removed = sorted({int(i) for i in speaker_ids})
    others = sorted({int(i) for i in against_speaker_ids} - set(removed))
    if not removed:
        return
    if others:
        existing = set(
            db.query(SpeakerCannotLink.speaker_id, SpeakerCannotLink.cannot_link_speaker_id)
            .filter(
                SpeakerCannotLink.speaker_id.in_(removed + others),
                SpeakerCannotLink.cannot_link_speaker_id.in_(removed + others),
            )
            .all()
        )
        pairs = {(int(a), int(b)) for a, b in existing}
        for s in removed:
            for o in others:
                for a, b in ((s, o), (o, s)):
                    if (a, b) not in pairs:
                        pairs.add((a, b))
                        db.add(
                            SpeakerCannotLink(speaker_id=a, cannot_link_speaker_id=b, reason=reason)
                        )
    if profile_id is not None:
        blocked = blacklisted_profile_ids(db, removed)
        for s in removed:
            if int(profile_id) not in blocked[s]:
                db.add(SpeakerProfileBlacklist(speaker_id=s, profile_id=profile_id, reason=reason))
    db.flush()


def lift_profile_blacklist(db: Session, speaker_id: int, profile_id: int) -> None:
    """The user explicitly named this one speaker as this profile: drop the correction.

    Removes its blacklist row for the profile and its cannot-link rows with the speakers
    linked to the profile. Only explicit single-speaker actions call this (typing the
    name, assigning or accepting that profile for that speaker); every automatic and
    bulk path honours the blacklist instead. Adds to the session; the caller commits.
    """
    from sqlalchemy import and_
    from sqlalchemy import or_

    sid, pid = int(speaker_id), int(profile_id)
    db.query(SpeakerProfileBlacklist).filter(
        SpeakerProfileBlacklist.speaker_id == sid, SpeakerProfileBlacklist.profile_id == pid
    ).delete(synchronize_session=False)
    members = [
        int(r[0])
        for r in db.query(Speaker.id).filter(Speaker.profile_id == pid, Speaker.id != sid).all()
    ]
    if members:
        db.query(SpeakerCannotLink).filter(
            or_(
                and_(
                    SpeakerCannotLink.speaker_id == sid,
                    SpeakerCannotLink.cannot_link_speaker_id.in_(members),
                ),
                and_(
                    SpeakerCannotLink.speaker_id.in_(members),
                    SpeakerCannotLink.cannot_link_speaker_id == sid,
                ),
            )
        ).delete(synchronize_session=False)
    db.flush()


def carry_blacklist(db: Session, source_id: int, target_id: int) -> None:
    """A merge folds the source's voice into the target, so its corrections come along.

    Without this the merged-away section, which the user said is not Bob, would sit in a
    target nothing forbids from becoming Bob. A profile the target is already linked to
    is not copied: the user merged into it, which is the explicit statement.
    """
    src, tgt = int(source_id), int(target_id)
    target_profile = db.query(Speaker.profile_id).filter(Speaker.id == tgt).scalar()
    for profile_id in sorted(blacklisted_profile_ids(db, [src])[src]):
        if target_profile is None or int(target_profile) != profile_id:
            record_unassignment(
                db, [tgt], against_speaker_ids=[], profile_id=profile_id, reason=REASON_MERGED
            )
    partners = cannot_link_partners(db, [src])[src] - {tgt}
    if partners:
        record_unassignment(
            db, [tgt], against_speaker_ids=partners, profile_id=None, reason=REASON_MERGED
        )
