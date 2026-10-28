"""A speaker's tenant is its FILE's, not the speaker row's own ``organization_id``.

The diarization pipeline creates ``Speaker`` rows without stamping ``organization_id``, so
every row in these tests is created the way production creates it (column left NULL) and
the tenant comes from ``media_file.organization_id``. Three readers used to look at the
speaker's own column:

* the profile rename (member selection by tenant),
* the segment speaker assignment (the platform-role check),
* ``require_speaker_access`` (the platform-role check).
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.core.tenancy import UNSCOPED
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.user import User
from app.schemas.transcript import SegmentSpeakerUpdate
from app.services.platform_access import TenancyMode
from app.services.platform_bypass import PlatformBypass
from app.services.speaker_profile_rename import apply_profile_name_to_speakers


def _user(db, label: str, role: str = "user") -> User:
    from app.core.security import get_password_hash

    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _org(db) -> Organization:
    org = Organization(
        external_org_id=f"org_tf_{uuid_pkg.uuid4().hex[:8]}", name="Tenant Org", is_active=True
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _file(db, owner: User, org_id: int | None) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        filename=f"tf_{file_uuid.hex[:8]}.mp4",
        storage_path=f"media/test/{file_uuid}.mp4",
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _speaker(db, owner: User, media_file: MediaFile, *, profile_id: int | None = None) -> Speaker:
    """A speaker as the pipeline creates it: ``organization_id`` is left NULL."""
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        profile_id=profile_id,
        name=f"SPEAKER_{uuid_pkg.uuid4().hex[:8]}",
    )
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    assert speaker.organization_id is None
    return speaker


class _RecordingBypass:
    """Stands in for ``PlatformBypass``; grants only the tenant ``granted_org_id``."""

    sees_all_in_scope = False

    def __init__(self, granted_org_id: int | None):
        self.granted_org_id = granted_org_id
        self.seen_org_ids: list[int | None] = []

    def allows(self, *, org_id, owner_id, need, resource_type, resource_uuid) -> bool:
        self.seen_org_ids.append(org_id)
        return bool(org_id == self.granted_org_id)


@pytest.fixture
def world(db_session):
    db = db_session
    owner = _user(db, "owner")
    stranger = _user(db, "stranger")
    org_a = _org(db)
    org_b = _org(db)
    profile = SpeakerProfile(user_id=owner.id, name="Original Name", uuid=uuid_pkg.uuid4())
    db.add(profile)
    db.commit()
    db.refresh(profile)

    files = {
        "a": _file(db, owner, org_a.id),
        "b": _file(db, owner, org_b.id),
        "personal": _file(db, owner, None),
    }
    speakers = {k: _speaker(db, owner, f, profile_id=profile.id) for k, f in files.items()}
    return {
        "db": db,
        "owner": owner,
        "stranger": stranger,
        "org_a": org_a,
        "org_b": org_b,
        "profile": profile,
        "files": files,
        "speakers": speakers,
    }


def _renamed(world) -> set[str]:
    world["db"].flush()
    return {key for key, s in world["speakers"].items() if s.display_name == "Renamed"}


class TestProfileRenameScopesByFileTenant:
    def test_unscoped_rename_reaches_every_member(self, world):
        """Control: with no tenant gate every linked speaker is rewritten."""
        apply_profile_name_to_speakers(
            world["db"], world["profile"].id, "Renamed", organization_id=UNSCOPED
        )
        assert _renamed(world) == {"a", "b", "personal"}

    def test_organization_rename_reaches_only_that_organizations_files(self, world):
        renames = apply_profile_name_to_speakers(
            world["db"], world["profile"].id, "Renamed", organization_id=world["org_a"].id
        )
        assert _renamed(world) == {"a"}
        assert [file_uuid for file_uuid, _ in renames] == [str(world["files"]["a"].uuid)]

    def test_personal_rename_never_reaches_an_organizations_files(self, world):
        apply_profile_name_to_speakers(
            world["db"], world["profile"].id, "Renamed", organization_id=None
        )
        assert _renamed(world) == {"personal"}

    def test_rename_stays_inside_the_owner_and_the_tenant(self, world):
        """The owner restriction and the tenant restriction compose; neither widens the other."""
        db = world["db"]
        other_speaker = _speaker(
            db, world["stranger"], world["files"]["a"], profile_id=world["profile"].id
        )
        apply_profile_name_to_speakers(
            db,
            world["profile"].id,
            "Renamed",
            restrict_to_user_id=world["owner"].id,
            organization_id=world["org_a"].id,
        )
        db.flush()
        assert world["speakers"]["a"].display_name == "Renamed"
        assert other_speaker.display_name != "Renamed"
        assert world["speakers"]["b"].display_name != "Renamed"


class TestSegmentSpeakerAssignmentUsesFileTenant:
    def _segment(self, world, key: str) -> TranscriptSegment:
        db = world["db"]
        segment = TranscriptSegment(
            media_file_id=world["files"][key].id, start_time=0.0, end_time=1.0, text="hello"
        )
        db.add(segment)
        db.commit()
        db.refresh(segment)
        return segment

    def _assign(self, world, key: str, bypass):
        from app.api.endpoints.transcript_segments import _get_new_speaker_id

        update = SegmentSpeakerUpdate(speaker_uuid=str(world["speakers"][key].uuid))
        return _get_new_speaker_id(
            world["db"],
            update,
            self._segment(world, key),
            world["stranger"],
            organization_id=world["org_a"].id,
            bypass=bypass,
        )

    def test_platform_check_receives_the_files_organization(self, world):
        bypass = _RecordingBypass(granted_org_id=world["org_a"].id)
        assert self._assign(world, "a", bypass) == world["speakers"]["a"].id
        assert bypass.seen_org_ids == [world["org_a"].id]

    def test_grant_for_another_organization_is_not_enough(self, world):
        bypass = _RecordingBypass(granted_org_id=world["org_b"].id)
        with pytest.raises(HTTPException) as exc:
            self._assign(world, "a", bypass)
        assert exc.value.status_code == 403
        assert bypass.seen_org_ids == [world["org_a"].id]

    def test_personal_file_is_checked_as_personal(self, world):
        """Control: a personal file's tenant is NULL both ways."""
        bypass = _RecordingBypass(granted_org_id=None)
        assert self._assign(world, "personal", bypass) == world["speakers"]["personal"].id
        assert bypass.seen_org_ids == [None]


class TestRequireSpeakerAccessUsesFileTenant:
    def _access(self, world, key: str, bypass):
        from app.utils.uuid_helpers import require_speaker_access

        return require_speaker_access(
            world["db"],
            world["speakers"][key].uuid,
            world["stranger"],
            organization_id=world["org_a"].id,
            bypass=bypass,
        )

    def test_platform_check_receives_the_files_organization(self, world):
        bypass = _RecordingBypass(granted_org_id=world["org_a"].id)
        assert self._access(world, "a", bypass).id == world["speakers"]["a"].id
        assert bypass.seen_org_ids == [world["org_a"].id]

    def test_grant_for_another_organization_does_not_open_the_speaker(self, world):
        bypass = _RecordingBypass(granted_org_id=world["org_b"].id)
        with pytest.raises(HTTPException) as exc:
            self._access(world, "a", bypass)
        assert exc.value.status_code == 403
        assert bypass.seen_org_ids == [world["org_a"].id]

    def test_personal_file_is_checked_as_personal(self, world):
        """Control: a personal file's tenant is NULL both ways."""
        bypass = _RecordingBypass(granted_org_id=None)
        assert self._access(world, "personal", bypass).id == world["speakers"]["personal"].id
        assert bypass.seen_org_ids == [None]


class TestCrossTenantAuditCarriesFileTenant:
    """The record written when the platform role acts across tenants names the file's tenant."""

    def _assign_as_platform_admin(self, world, key: str, monkeypatch) -> list[dict]:
        from app.api.endpoints.transcript_segments import _get_new_speaker_id
        from app.auth.audit import audit_logger

        events: list[dict] = []
        monkeypatch.setattr(
            audit_logger, "log", lambda event_type, outcome, **kw: events.append(kw)
        )
        db = world["db"]
        admin = _user(db, "platform", role="admin")
        segment = TranscriptSegment(
            media_file_id=world["files"][key].id, start_time=0.0, end_time=1.0, text="hello"
        )
        db.add(segment)
        db.commit()
        bypass = PlatformBypass(
            user=admin, mode=TenancyMode.SINGLE, caller_org_id=None, audit_cross_tenant=True
        )
        _get_new_speaker_id(
            db,
            SegmentSpeakerUpdate(speaker_uuid=str(world["speakers"][key].uuid)),
            segment,
            admin,
            bypass=bypass,
        )
        return events

    def test_event_for_an_organization_file_names_the_organization(self, world, monkeypatch):
        events = self._assign_as_platform_admin(world, "a", monkeypatch)
        assert [e["organization_id"] for e in events] == [world["org_a"].id]
        assert events[0]["details"]["resource_type"] == "speaker"

    def test_event_for_a_personal_file_has_no_organization(self, world, monkeypatch):
        """Control: a personal file's tenant is NULL both ways."""
        events = self._assign_as_platform_admin(world, "personal", monkeypatch)
        assert [e["organization_id"] for e in events] == [None]


class TestKnownSpeakersAreListedForTheFilesTenant:
    """Profile names sent to the LLM provider come only from the file's tenant."""

    def _names(self, world, organization_id) -> set[str]:
        from app.tasks.speaker_identification_task import _get_known_speakers

        return {
            k["name"] for k in _get_known_speakers(world["db"], world["owner"].id, organization_id)
        }

    @pytest.fixture
    def profiles(self, world):
        db = world["db"]
        names = {}
        for key, org_id in (
            ("personal", None),
            ("a", world["org_a"].id),
            ("b", world["org_b"].id),
        ):
            name = f"Known {key} {uuid_pkg.uuid4().hex[:6]}"
            db.add(
                SpeakerProfile(
                    user_id=world["owner"].id,
                    organization_id=org_id,
                    name=name,
                    uuid=uuid_pkg.uuid4(),
                )
            )
            names[key] = name
        db.add(
            SpeakerProfile(
                user_id=world["stranger"].id,
                organization_id=None,
                name="Someone Else",
                uuid=uuid_pkg.uuid4(),
            )
        )
        db.commit()
        return names

    def test_personal_file_lists_only_org_less_profiles_of_the_user(self, world, profiles):
        names = self._names(world, None)
        assert profiles["personal"] in names
        assert {profiles["a"], profiles["b"], "Someone Else"}.isdisjoint(names)

    def test_organization_file_lists_only_that_organizations_profiles(self, world, profiles):
        names = self._names(world, world["org_a"].id)
        assert profiles["a"] in names
        assert {profiles["personal"], profiles["b"], "Someone Else"}.isdisjoint(names)
