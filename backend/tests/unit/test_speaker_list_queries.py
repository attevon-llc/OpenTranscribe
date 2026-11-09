"""Service-level behaviour of the speaker list, picker, occurrence and segment queries."""

from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlalchemy import true

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services import speaker_list_queries as lists


def _file(db, owner, **kw) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename=f"{uuid.uuid4().hex[:6]}.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status=kw.pop("status", "completed"),
        **kw,
    )
    db.add(mf)
    db.flush()
    return mf


def _speaker(db, owner, mf, **kw) -> Speaker:
    spk = Speaker(
        user_id=owner.id,
        media_file_id=mf.id,
        name=kw.pop("name", f"S_{uuid.uuid4().hex[:6]}"),
        **kw,
    )
    db.add(spk)
    db.flush()
    return spk


def _segments(db, mf, speaker, starts_and_lengths):
    rows = [
        TranscriptSegment(
            media_file_id=mf.id,
            speaker_id=speaker.id,
            start_time=start,
            end_time=start + length,
            text="t",
        )
        for start, length in starts_and_lengths
    ]
    db.add_all(rows)
    db.flush()
    return rows


def test_list_speakers_page_orders_by_speaker_number_and_pages(db_session, normal_user):
    mf = _file(db_session, normal_user)
    for n in (10, 2, 1):
        _speaker(db_session, normal_user, mf, name=f"SPEAKER_{n:02d}")
    _speaker(db_session, normal_user, mf, name="Zed")
    mine = [Speaker.user_id == normal_user.id]
    page = lists.list_speakers_page(
        db_session, mine, verified_only=False, file_id=None, skip=0, page_size=3
    )
    assert [s.name for s in page] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_10"]
    rest = lists.list_speakers_page(
        db_session, mine, verified_only=False, file_id=None, skip=3, page_size=3
    )
    assert [s.name for s in rest] == ["Zed"]


def test_list_speakers_page_for_one_file_ignores_paging_and_honours_verified(
    db_session, normal_user
):
    mf = _file(db_session, normal_user)
    verified = _speaker(db_session, normal_user, mf, verified=True)
    _speaker(db_session, normal_user, mf, verified=False)
    everything = lists.list_speakers_page(
        db_session, [], verified_only=False, file_id=mf.id, skip=5, page_size=1
    )
    assert len(everything) == 2
    only = lists.list_speakers_page(
        db_session, [], verified_only=True, file_id=mf.id, skip=0, page_size=1
    )
    assert only == [verified]


def test_profile_id_for_uuid_applies_owner_and_tenant_limits(db_session, normal_user, admin_user):
    prof = SpeakerProfile(user_id=normal_user.id, name=f"P{uuid.uuid4().hex[:6]}")
    db_session.add(prof)
    db_session.flush()
    find = lists.profile_id_for_uuid
    assert find(db_session, str(prof.uuid), owner_user_id=None, tenant_pred=None) == prof.id
    assert (
        find(db_session, str(prof.uuid), owner_user_id=normal_user.id, tenant_pred=true())
        == prof.id
    )
    assert find(db_session, str(prof.uuid), owner_user_id=admin_user.id, tenant_pred=None) is None
    assert find(db_session, str(uuid.uuid4()), owner_user_id=None, tenant_pred=None) is None


def test_segment_helpers_count_and_rank_per_speaker(db_session, normal_user):
    mf = _file(db_session, normal_user)
    a, b = _speaker(db_session, normal_user, mf), _speaker(db_session, normal_user, mf)
    _segments(db_session, mf, a, [(float(i), 1.0) for i in range(6)])
    _segments(db_session, mf, b, [(100.0, 1.0)])
    assert lists.segment_counts_by_speaker(db_session, [a.id, b.id]) == {a.id: 6, b.id: 1}
    assert lists.segment_counts_by_speaker(db_session, []) == {}
    stamps = lists.first_segment_timestamps_by_speaker(db_session, [a.id, b.id], mf.id)
    assert [s["start_time"] for s in stamps[a.id]] == [0.0, 1.0, 2.0, 3.0]
    assert [s["segment_index"] for s in stamps[b.id]] == [6]
    assert lists.first_segment_timestamps_by_speaker(db_session, [], mf.id) == {}


def test_speakers_named_excludes_the_given_speaker_and_other_owners(
    db_session, normal_user, admin_user
):
    mf = _file(db_session, normal_user)
    me, twin = (_speaker(db_session, normal_user, mf, display_name="Dana") for _ in range(2))
    _speaker(db_session, admin_user, _file(db_session, admin_user), display_name="Dana")
    found = lists.speakers_named(
        db_session,
        "Dana",
        excluding_speaker_id=me.id,
        owner_user_id=normal_user.id,
        tenant_pred=None,
        include_quarantined=False,
    )
    assert found == [twin]
    everyone = lists.speakers_named(
        db_session,
        "Dana",
        excluding_speaker_id=None,
        owner_user_id=None,
        tenant_pred=None,
        include_quarantined=False,
    )
    assert len(everyone) == 3


def test_speakers_linked_to_profile_drops_quarantined_unless_exempt(db_session, normal_user):
    prof = SpeakerProfile(user_id=normal_user.id, name=f"P{uuid.uuid4().hex[:6]}")
    db_session.add(prof)
    db_session.flush()
    ok_file = _file(db_session, normal_user)
    quarantined = _file(db_session, normal_user, is_quarantined=True)
    keep = _speaker(db_session, normal_user, ok_file, profile_id=prof.id)
    hidden = _speaker(db_session, normal_user, quarantined, profile_id=prof.id)

    def linked(include_quarantined):
        return lists.speakers_linked_to_profile(
            db_session,
            prof.id,
            owner_user_id=normal_user.id,
            tenant_pred=None,
            include_quarantined=include_quarantined,
        )

    assert linked(False) == [keep]
    assert set(linked(True)) == {keep, hidden}
    unfiltered = linked(None)
    assert set(unfiltered) == {keep, hidden}


def test_speaker_picker_groups_label_by_name_and_count_files(db_session, normal_user):
    a_file, b_file = _file(db_session, normal_user), _file(db_session, normal_user)
    _speaker(db_session, normal_user, a_file, display_name="Robin")
    _speaker(db_session, normal_user, b_file, display_name="Robin")
    _speaker(db_session, normal_user, a_file, display_name="Sam")
    found = lists.speaker_picker_groups(
        db_session,
        [Speaker.user_id == normal_user.id],
        label=Speaker.display_name.expression,
        is_unnamed=func.bool_and(Speaker.display_name.is_(None)),
        visible_profile=Speaker.profile_id.expression,
        limit=10,
    )
    assert [(g.label, g.media_count, g.is_unnamed, g.profile_uuid) for g in found] == [
        ("Robin", 2, False, None),
        ("Sam", 1, False, None),
    ]
