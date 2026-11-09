"""Service-level behaviour of the speaker row lookups and label selections."""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerMatch
from app.models.speaker import SpeakerProfile
from app.services import speaker_label_queries
from app.services import speaker_lookups


def _file(db, owner, *, organization_id=None) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename=f"{uuid.uuid4().hex[:6]}.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
        organization_id=organization_id,
    )
    db.add(mf)
    db.flush()
    return mf


def _speaker(db, owner, mf, **kw) -> Speaker:
    spk = Speaker(
        user_id=owner.id,
        media_file_id=mf.id,
        name=kw.pop("name", f"SPEAKER_{uuid.uuid4().hex[:6]}"),
        **kw,
    )
    db.add(spk)
    db.flush()
    return spk


def _profile(db, owner, name) -> SpeakerProfile:
    prof = SpeakerProfile(user_id=owner.id, name=name)
    db.add(prof)
    db.flush()
    return prof


def test_speaker_and_file_lookups_answer_none_for_missing_rows(db_session, normal_user):
    mf = _file(db_session, normal_user)
    spk = _speaker(db_session, normal_user, mf)
    assert speaker_lookups.speaker_by_id(db_session, spk.id) is spk
    assert speaker_lookups.speaker_file_id(db_session, spk.id) == mf.id
    assert speaker_lookups.file_filename(db_session, mf.id) == mf.filename
    assert speaker_lookups.file_uuid(db_session, mf.id) == str(mf.uuid)
    assert speaker_lookups.file_organization_id(db_session, mf.id) is None
    missing = 2_000_000_000
    assert speaker_lookups.speaker_by_id(db_session, missing) is None
    assert speaker_lookups.speaker_file_id(db_session, missing) is None
    assert speaker_lookups.file_filename(db_session, missing) is None
    assert speaker_lookups.file_uuid(db_session, missing) is None
    assert speaker_lookups.profile_uuid(db_session, missing) is None


def test_speakers_by_id_keys_rows_by_id(db_session, normal_user):
    mf = _file(db_session, normal_user)
    a, b = _speaker(db_session, normal_user, mf), _speaker(db_session, normal_user, mf)
    found = speaker_lookups.speakers_by_id(db_session, [a.id, b.id])
    assert found == {a.id: a, b.id: b}


def test_profile_name_lookups_differ_in_how_they_compare(db_session, normal_user):
    prof = _profile(db_session, normal_user, "Ada Lovelace")
    look = speaker_lookups
    assert look.profile_by_exact_name(db_session, normal_user.id, "Ada Lovelace", None) is prof
    assert look.profile_by_exact_name(db_session, normal_user.id, "ada lovelace", None) is None
    assert look.profile_by_name_pattern(db_session, normal_user.id, "ada lovelace", None) is prof
    assert look.profile_id_by_lowercased_name(db_session, normal_user.id, "ada lovelace", None) == (
        prof.id
    )


def test_profile_name_lookups_ignore_another_tenant(db_session, normal_user):
    _profile(db_session, normal_user, "Grace")
    other_tenant = 2_000_000_001
    assert (
        speaker_lookups.profile_by_exact_name(db_session, normal_user.id, "Grace", other_tenant)
        is None
    )
    assert (
        speaker_lookups.profile_id_by_lowercased_name(
            db_session, normal_user.id, "grace", other_tenant
        )
        is None
    )


def test_profile_named_like_falls_back_to_own_profiles_without_an_accessible_list(
    db_session, normal_user, admin_user
):
    mine = _profile(db_session, normal_user, "Marie Curie")
    theirs = _profile(db_session, admin_user, "Marie Antoinette")
    find = speaker_lookups.profile_named_like
    assert find(db_session, "curie", accessible_profile_ids=None, user_id=normal_user.id) is mine
    assert (
        find(db_session, "antoinette", accessible_profile_ids=None, user_id=normal_user.id) is None
    )
    assert (
        find(db_session, "antoinette", accessible_profile_ids=[theirs.id], user_id=normal_user.id)
        is theirs
    )


def test_speaker_labels_for_profile_lists_linked_speakers(db_session, normal_user):
    prof = _profile(db_session, normal_user, "Linus")
    mf = _file(db_session, normal_user)
    linked = _speaker(db_session, normal_user, mf, profile_id=prof.id, display_name="Linus T")
    _speaker(db_session, normal_user, mf)
    assert speaker_lookups.speaker_labels_for_profile(db_session, prof.id) == [
        (linked.uuid, "Linus T")
    ]


def test_clusters_promoted_to_holding_requires_promotion_and_membership(db_session, normal_user):
    prof = _profile(db_session, normal_user, "Cluster Owner")
    mf = _file(db_session, normal_user)
    spk = _speaker(db_session, normal_user, mf)
    cluster = SpeakerCluster(user_id=normal_user.id, promoted_to_profile_id=prof.id)
    db_session.add(cluster)
    db_session.flush()
    spk.cluster_id = cluster.id
    db_session.flush()
    assert speaker_lookups.clusters_promoted_to_holding(db_session, prof.id, spk.id) == [cluster]
    other = _profile(db_session, normal_user, "Elsewhere")
    assert speaker_lookups.clusters_promoted_to_holding(db_session, other.id, spk.id) == []


def test_speaker_match_for_pair_uses_the_ordered_pair(db_session, normal_user):
    mf = _file(db_session, normal_user)
    a, b = _speaker(db_session, normal_user, mf), _speaker(db_session, normal_user, mf)
    lo, hi = sorted((a.id, b.id))
    match = SpeakerMatch(speaker1_id=lo, speaker2_id=hi, confidence=0.9)
    db_session.add(match)
    db_session.flush()
    assert speaker_lookups.speaker_match_for_pair(db_session, lo, hi) is match
    assert speaker_lookups.speaker_match_for_pair(db_session, hi, lo) is None


def test_reassign_transcript_segments_moves_only_the_source_speakers_segments(
    db_session, normal_user
):
    mf = _file(db_session, normal_user)
    src, dst, bystander = (_speaker(db_session, normal_user, mf) for _ in range(3))
    segs = [
        TranscriptSegment(
            media_file_id=mf.id, speaker_id=s.id, start_time=i, end_time=i + 1, text="t"
        )
        for i, s in enumerate((src, src, bystander))
    ]
    db_session.add_all(segs)
    db_session.flush()
    speaker_lookups.reassign_transcript_segments(db_session, src.id, dst.id)
    db_session.expire_all()
    assert [s.speaker_id for s in segs] == [dst.id, dst.id, bystander.id]


def test_medium_confidence_suggestions_keeps_only_the_band_unverified(db_session, normal_user):
    mf = _file(db_session, normal_user)
    kw = {"suggested_name": "Pat"}
    inside = _speaker(db_session, normal_user, mf, confidence=0.6, **kw)
    _speaker(db_session, normal_user, mf, confidence=0.49, **kw)
    _speaker(db_session, normal_user, mf, confidence=0.75, **kw)
    _speaker(db_session, normal_user, mf, confidence=0.6, verified=True, **kw)
    _speaker(db_session, normal_user, mf, confidence=0.6, suggested_name="Other")
    found = speaker_label_queries.medium_confidence_suggestions(
        db_session, normal_user.id, None, "Pat"
    )
    assert found == [inside]


def test_candidate_speaker_rows_exclude_the_trigger_and_other_users(
    db_session, normal_user, admin_user
):
    mf = _file(db_session, normal_user)
    trigger = _speaker(db_session, normal_user, mf)
    other = _speaker(db_session, normal_user, mf)
    _speaker(db_session, admin_user, _file(db_session, admin_user))
    rows = speaker_label_queries.candidate_speaker_rows(
        db_session, normal_user.id, trigger.id, None
    )
    assert [r.id for r in rows] == [other.id]


def test_set_gender_updates_linked_speakers_of_the_user_only(db_session, normal_user, admin_user):
    prof = _profile(db_session, normal_user, "Gen")
    mf = _file(db_session, normal_user)
    mine = _speaker(db_session, normal_user, mf, profile_id=prof.id)
    theirs = _speaker(db_session, admin_user, _file(db_session, admin_user), profile_id=prof.id)
    count = speaker_label_queries.set_gender_on_profile_speakers(
        db_session, prof.id, normal_user.id, None, "female"
    )
    db_session.refresh(mine)
    db_session.refresh(theirs)
    assert count == 1
    assert (mine.predicted_gender, mine.gender_confirmed_by_user) == ("female", True)
    assert theirs.predicted_gender != "female"


def test_attribute_migration_counts_files_with_unpredicted_speakers(db_session, normal_user):
    base_total, base_pending = speaker_label_queries.attribute_migration_counts(db_session)
    predicted = _file(db_session, normal_user)
    _speaker(db_session, normal_user, predicted, attributes_predicted_at=datetime.now(UTC))
    pending = _file(db_session, normal_user)
    _speaker(db_session, normal_user, pending)
    _file(db_session, normal_user)
    assert speaker_label_queries.attribute_migration_counts(db_session) == (
        base_total + 2,
        base_pending + 1,
    )
