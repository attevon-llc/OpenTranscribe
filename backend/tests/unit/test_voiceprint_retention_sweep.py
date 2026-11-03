"""Opt-in voiceprint retention: selection and dispatch (``tasks/voiceprint_retention.py``).

The purge itself (``PurgeScope(kind="files")``) is another branch's interface; here the dispatch
seam is injected, so these tests pin the SELECTION rule and the off/unavailable states.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import text

from app.core.constants import PRIVACY_VOICEPRINT_RETENTION_MONTHS_KEY
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.tasks import voiceprint_retention as vr

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_OLD = _NOW - timedelta(days=900)
_RECENT = _NOW - timedelta(days=10)


def _file(db, user, *, uploaded=_OLD, held=False) -> MediaFile:
    f = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="vr.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
        legal_hold=held,
    )
    db.add(f)
    db.flush()
    db.execute(
        text("UPDATE media_file SET upload_time = :t WHERE id = :i"), {"t": uploaded, "i": f.id}
    )
    return f


def _profile(db, user, used_at) -> SpeakerProfile:
    p = SpeakerProfile(user_id=user.id, name=f"vr-{uuid_pkg.uuid4().hex[:8]}")
    db.add(p)
    db.flush()
    db.execute(
        text("UPDATE speaker_profile SET last_interaction_at = :t WHERE id = :i"),
        {"t": used_at, "i": p.id},
    )
    return p


def _speaker(db, user, f, profile=None) -> Speaker:
    s = Speaker(
        media_file_id=f.id,
        user_id=user.id,
        name=f"SPEAKER_{uuid_pkg.uuid4().hex[:6]}",
        profile_id=profile.id if profile else None,
    )
    db.add(s)
    db.flush()
    return s


def _always(_db, _fid):
    return True


def _set_months(db, value: str) -> None:
    from app.services.system_settings_service import set_setting

    set_setting(db, PRIVACY_VOICEPRINT_RETENTION_MONTHS_KEY, value)


def _due(db, cutoff) -> set[int]:
    return set(vr.due_file_ids(db, cutoff, limit=10_000, has_voiceprints=_always))


def test_off_by_default_and_at_zero(db_session):
    from app.core.constants import DEFAULT_PRIVACY_VOICEPRINT_RETENTION_MONTHS

    assert DEFAULT_PRIVACY_VOICEPRINT_RETENTION_MONTHS == 0
    _set_months(db_session, "0")
    calls: list[list[int]] = []

    def dispatch(ids: list[int]) -> str:
        calls.append(ids)
        return "never"

    out = vr.run_sweep(db_session, now=_NOW, dispatch=dispatch, scope_supported=lambda: True)
    assert out == {"status": "disabled", "retention_months": 0}
    assert calls == []


def test_without_a_files_scope_nothing_is_dispatched(db_session):
    _set_months(db_session, "24")
    calls: list[list[int]] = []

    def dispatch(ids: list[int]) -> str:
        calls.append(ids)
        return "never"

    out = vr.run_sweep(db_session, now=_NOW, dispatch=dispatch, scope_supported=lambda: False)
    assert out["status"] == "unavailable"
    assert calls == []


def test_the_selection_rule(db_session, normal_user):
    cutoff = vr.cutoff_for(24, _NOW)
    stale, fresh = (
        _profile(db_session, normal_user, _OLD),
        _profile(db_session, normal_user, _RECENT),
    )

    due_unlinked = _file(db_session, normal_user)
    _speaker(db_session, normal_user, due_unlinked)
    due_stale = _file(db_session, normal_user)
    _speaker(db_session, normal_user, due_stale, stale)
    protected = _file(db_session, normal_user)  # one fresh profile protects the whole file
    _speaker(db_session, normal_user, protected, stale)
    _speaker(db_session, normal_user, protected, fresh)
    too_new = _file(db_session, normal_user, uploaded=_RECENT)
    _speaker(db_session, normal_user, too_new, stale)
    held = _file(db_session, normal_user, held=True)
    _speaker(db_session, normal_user, held, stale)
    no_speakers = _file(db_session, normal_user)

    got = _due(db_session, cutoff)
    assert {due_unlinked.id, due_stale.id} <= got
    assert got.isdisjoint({protected.id, too_new.id, held.id, no_speakers.id})


def test_an_unmeasurable_or_already_purged_file_is_skipped(db_session, normal_user):
    cutoff = vr.cutoff_for(24, _NOW)
    f = _file(db_session, normal_user)
    _speaker(db_session, normal_user, f)
    answers: tuple[bool | None, ...] = (False, None)
    for answer in answers:

        def measured(_d: object, _i: int, a: bool | None = answer) -> bool | None:
            return a

        got = vr.due_file_ids(db_session, cutoff, limit=10_000, has_voiceprints=measured)
        assert f.id not in got


def test_stale_profiles_left_empty_are_listed_not_deleted(db_session, normal_user):
    _set_months(db_session, "24")
    stale = _profile(db_session, normal_user, _OLD)
    f = _file(db_session, normal_user)
    _speaker(db_session, normal_user, f, stale)
    calls: list[list[int]] = []

    def dispatch(ids):
        calls.append(ids)
        return "task-1"

    out = vr.run_sweep(
        db_session,
        now=_NOW,
        dispatch=dispatch,
        has_voiceprints=lambda _d, i: i == f.id,
        scope_supported=lambda: True,
    )
    assert out["status"] == "ok" and calls == [[f.id]]
    assert str(stale.uuid) in out["stale_profiles_left_empty"]
    assert db_session.get(SpeakerProfile, stale.id) is not None


def test_the_batch_is_bounded(db_session, normal_user):
    cutoff = vr.cutoff_for(24, _NOW)
    oldest = datetime(2000, 1, 1, tzinfo=UTC)  # ahead of any other file in the candidate order
    files = [_file(db_session, normal_user, uploaded=oldest) for _ in range(3)]
    for f in files:
        _speaker(db_session, normal_user, f)
    ids = {f.id for f in files}
    got = vr.due_file_ids(db_session, cutoff, limit=2, has_voiceprints=lambda _d, i: i in ids)
    assert len(got) == 2 and set(got) <= ids
