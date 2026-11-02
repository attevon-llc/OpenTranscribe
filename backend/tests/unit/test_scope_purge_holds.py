"""User, organization and instance voiceprint purges leave held and quarantined files alone.

Legal hold always wins: a switch-off purge deletes the voiceprints of every OTHER file, leaves the
voiceprints of a platform-held, organization-held or quarantined file in place, and says how many
files it skipped (counts only; an organization hold's uuid is never in a response or audit event).
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest

from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services.privacy import purge
from app.services.privacy.purge import PurgeScope
from app.services.privacy.purge_file_guard import scope_protection
from app.services.privacy.purge_residual import measure_voiceprint_residual
from app.services.privacy.purge_voiceprints import VoiceprintTargets
from app.services.privacy.purge_voiceprints import org_stamp_clause
from app.services.privacy.purge_voiceprints import purge_voiceprints_postgres
from app.tasks.privacy_purge_task import privacy_purge_task
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_file as _make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class _KeepOpen:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


@dataclass
class World:
    scope: PurgeScope
    normal: Speaker
    platform: Speaker
    org_held: Speaker
    quarantined: Speaker
    shared_profile: SpeakerProfile
    plain_profile: SpeakerProfile
    hold: ContentHold


KINDS = ["user", "organization", "instance_all"]


def _scope(kind, user, org):
    if kind == "organization":
        return PurgeScope("organization", organization_id=org.id)
    if kind == "user":
        return PurgeScope("user", user_id=user.id)
    return PurgeScope("instance_all")


def _file(db, user, org):
    mf = _make_file(db, user)
    mf.storage_path = f"user_{user.id}/{mf.uuid}.wav"
    mf.organization_id = org.id if org is not None else None
    db.flush()
    return mf


def _profile(db, user, org, name, count):
    row = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name=name, embedding_count=count)
    row.organization_id = org.id if org is not None else None
    db.add(row)
    db.flush()
    return row


def _voice_speaker(db, user, mf, profile, name="SPEAKER_00"):
    return make_speaker(
        db,
        user,
        mf,
        name=name,
        display_name="Voice Name",
        profile_id=profile.id,
        identity_source="voiceprint",
        verified=False,
    )


def _world(db, user, kind: str) -> World:
    org = make_org(db) if kind == "organization" else None
    hold_org = org or make_org(db)
    files = [_file(db, user, org) for _ in range(4)]
    files[1].legal_hold = True
    hold = ContentHold(
        organization_id=hold_org.id, scope="file", reason="pending litigation matter"
    )
    db.add(hold)
    db.flush()
    db.add(ContentHoldFile(hold_id=hold.id, media_file_id=files[2].id))
    files[3].is_quarantined = True
    shared = _profile(db, user, org, "Shared", 3)
    plain = _profile(db, user, org, "Plain", 2)
    only_normal = _profile(db, user, org, "Only", 1)
    normal = _voice_speaker(db, user, files[0], shared)
    platform = _voice_speaker(db, user, files[1], shared)
    org_held = _voice_speaker(db, user, files[2], plain)
    quarantined = _voice_speaker(db, user, files[3], plain)
    _voice_speaker(db, user, files[0], only_normal, name="SPEAKER_01")
    db.commit()
    return World(
        _scope(kind, user, org), normal, platform, org_held, quarantined, shared, plain, hold
    )


@pytest.mark.parametrize("kind", KINDS)
def test_protected_files_keep_their_voiceprints_and_the_rest_are_purged(
    db_session, normal_user, kind
):
    baseline_org = make_org(db_session) if kind == "organization" else None
    before = scope_protection(db_session, _scope(kind, normal_user, baseline_org))
    w = _world(db_session, normal_user, kind)

    done = purge_voiceprints_postgres(db_session, w.scope)
    db_session.expire_all()

    assert db_session.get(Speaker, w.normal.id).display_name is None, "normal file is purged"
    for kept in (w.platform, w.org_held, w.quarantined):
        row = db_session.get(Speaker, kept.id)
        assert (row.display_name, row.identity_source) == ("Voice Name", "voiceprint")
        assert row.profile_id is not None
    assert str(w.normal.uuid) in done.targets.speaker_uuids
    for kept in (w.platform, w.org_held, w.quarantined):
        assert str(kept.uuid) not in done.targets.speaker_uuids
    assert done.counts["files_skipped_held"] - before.files_held == 2
    assert done.counts["files_skipped_quarantined"] - before.files_quarantined == 1


@pytest.mark.parametrize("kind", ["user", "organization"])
def test_a_profile_that_keeps_a_protected_voice_is_retained_not_reset(
    db_session, normal_user, kind
):
    w = _world(db_session, normal_user, kind)

    done = purge_voiceprints_postgres(db_session, w.scope)
    db_session.expire_all()

    assert db_session.get(SpeakerProfile, w.plain_profile.id).embedding_count == 2
    assert db_session.get(SpeakerProfile, w.shared_profile.id).embedding_count == 3
    assert done.counts["profiles_retained"] == 2
    assert done.counts["profiles_reset"] == 1, "only the profile fed by the purged file alone"
    assert {w.plain_profile.id, w.shared_profile.id} <= set(done.targets.recompute_profile_ids)
    assert done.targets.keeps_protected is True


@pytest.mark.parametrize("kind", ["user", "organization"])
def test_residual_agrees_with_the_purge_and_reports_no_false_leftovers(
    db_session, normal_user, kind
):
    w = _world(db_session, normal_user, kind)
    done = purge_voiceprints_postgres(db_session, w.scope)

    residual = measure_voiceprint_residual(
        w.scope,
        VoiceprintTargets(keeps_protected=done.targets.keeps_protected),
        session_factory=lambda: cast("Session", _KeepOpen(db_session)),
        errors=[],
    )

    assert residual.counts["speaker_identities_machine"] == 0
    assert residual.counts["profiles_with_embeddings"] == 0


def test_org_stamp_sweep_is_skipped_while_protected_voices_remain():
    scope = PurgeScope("organization", organization_id=1)

    assert org_stamp_clause(scope, "speaker") is not None
    assert org_stamp_clause(scope, "speaker", keeps_protected=True) is None


@pytest.mark.parametrize("kind", ["user", "organization"])
def test_with_nothing_protected_everything_is_purged_and_nothing_is_reported(
    db_session, normal_user, kind
):
    org = make_org(db_session) if kind == "organization" else None
    scope = _scope(kind, normal_user, org)
    profile = _profile(db_session, normal_user, org, "p", 1)
    sp = _voice_speaker(db_session, normal_user, _file(db_session, normal_user, org), profile)
    db_session.commit()

    done = purge_voiceprints_postgres(db_session, scope)
    db_session.expire_all()

    assert db_session.get(Speaker, sp.id).display_name is None
    assert done.counts["files_skipped_held"] == 0
    assert done.counts["files_skipped_quarantined"] == 0
    assert done.counts["profiles_retained"] == 0
    assert done.targets.keeps_protected is False


@pytest.fixture
def task_seams(db_session, monkeypatch):
    """The task run in process: the store seams (OpenSearch, Redis record, audit sink) recorded."""
    records: dict[str, Any] = {}
    audits: list[dict[str, Any]] = []

    @contextmanager
    def session_scope():
        yield db_session

    def write_record(task_id, user_id, counts, **kw):
        records.clear()
        records.update(counts=counts, **kw)

    def no_residual(*a, **k):
        return purge.Residual(counts={key: 0 for key in purge.VOICEPRINT_RESIDUAL_KEYS})

    replaced: dict[Any, dict[str, Any]] = {
        "app.tasks.privacy_purge_task": {
            "session_scope": session_scope,
            "SessionLocal": lambda: _KeepOpen(db_session),
        },
        purge: {
            "targets_still_enabled": lambda *a, **k: [],
            "purge_voiceprint_docs": lambda *a, **k: {},
            "recompute_profiles": lambda *a, **k: 0,
            "rebuild_trimmed_clusters": lambda *a, **k: [],
            "refresh_cleared_speakers": lambda *a, **k: {
                "statuses_refreshed": 0,
                "reindex_queue_failed": 0,
            },
            "write_record": write_record,
            "read_record": lambda task_id: dict(records) or None,
            "measure_voiceprint_residual": no_residual,
        },
        purge.audit_logger: {"log": lambda **kw: audits.append(kw["details"])},
        privacy_purge_task: {"apply_async": lambda *a, **k: None},
    }
    for target, attrs in replaced.items():
        for name, value in attrs.items():
            monkeypatch.setattr(target, name, value)
    return records, audits


@pytest.mark.parametrize("kind", KINDS)
def test_task_reports_skipped_counts_once_and_never_the_hold_uuid(
    db_session, normal_user, task_seams, kind
):
    w = _world(db_session, normal_user, kind)
    records, audits = task_seams

    results = [
        privacy_purge_task.run(w.scope.to_payload(), ["voiceprints"], None, phase=phase)
        for phase in (1, 2)
    ]

    assert [r["status"] for r in results] == ["completed", "completed"], results
    postgres = records["counts"]["postgres"]
    first = next(a for a in audits if a["phase"] == "pass_1_completed")
    one_pass = first["counts"]["postgres"]
    assert one_pass["files_skipped_held"] >= 2
    assert one_pass["files_skipped_quarantined"] >= 1
    assert postgres["files_skipped_held"] == one_pass["files_skipped_held"], "not doubled"
    assert str(w.hold.uuid) not in json.dumps([records, audits], default=str)
