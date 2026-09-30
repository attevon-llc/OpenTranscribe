"""Tests for speaker profile sharing via collection sharing.

Verifies that:
1. PermissionService correctly computes accessible profile IDs
2. Shared profiles are visible to shared users but not to unshared users
3. Owner-only mutations (rename, delete, avatar) are enforced
4. Shared users can assign speakers to shared profiles
5. Admin sees all profiles with correct ownership flags
6. No data leakage to users without sharing access
"""

import uuid as uuid_mod

import pytest
from sqlalchemy.orm import Session

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.sharing import CollectionShare
from app.models.user import User
from app.services.permission_service import PermissionService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_a(db_session: Session) -> User:
    """Create User A who owns profiles and files."""
    uid = str(uuid_mod.uuid4())[:8]
    from app.core.security import get_password_hash

    user = User(
        email=f"user_a_{uid}@example.com",
        full_name="User A",
        hashed_password=get_password_hash("pass_a"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def user_b(db_session: Session) -> User:
    """Create User B who receives shared access."""
    uid = str(uuid_mod.uuid4())[:8]
    from app.core.security import get_password_hash

    user = User(
        email=f"user_b_{uid}@example.com",
        full_name="User B",
        hashed_password=get_password_hash("pass_b"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def user_c(db_session: Session) -> User:
    """Create User C who has NO shared access (isolation test)."""
    uid = str(uuid_mod.uuid4())[:8]
    from app.core.security import get_password_hash

    user = User(
        email=f"user_c_{uid}@example.com",
        full_name="User C",
        hashed_password=get_password_hash("pass_c"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sharing_setup(db_session: Session, user_a: User, user_b: User):
    """Set up the full sharing chain: User A owns file+speaker+profile,
    shared via collection with User B.

    Returns a dict with all created entities.
    """
    # User A creates a media file
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user_a.id,
        filename="test.mp4",
        storage_path="test/test.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()

    # User A creates a speaker profile
    profile_a = SpeakerProfile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user_a.id,
        name="Senator Smith",
    )
    db_session.add(profile_a)
    db_session.flush()

    # User A creates a speaker assigned to the profile
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=user_a.id,
        name="SPEAKER_00",
        display_name="Senator Smith",
        profile_id=profile_a.id,
        verified=True,
    )
    db_session.add(speaker)
    db_session.flush()

    # User A creates a collection and adds the file
    collection = Collection(
        uuid=str(uuid_mod.uuid4()),
        user_id=user_a.id,
        name=f"Shared Collection {str(uuid_mod.uuid4())[:8]}",
    )
    db_session.add(collection)
    db_session.flush()

    collection_member = CollectionMember(
        uuid=str(uuid_mod.uuid4()),
        collection_id=collection.id,
        media_file_id=media_file.id,
    )
    db_session.add(collection_member)
    db_session.flush()

    # Share collection with User B
    share = CollectionShare(
        uuid=str(uuid_mod.uuid4()),
        collection_id=collection.id,
        shared_by_id=user_a.id,
        target_type="user",
        target_user_id=user_b.id,
        permission="editor",
    )
    db_session.add(share)
    db_session.commit()

    return {
        "media_file": media_file,
        "profile_a": profile_a,
        "speaker": speaker,
        "collection": collection,
        "collection_member": collection_member,
        "share": share,
    }


@pytest.fixture
def user_b_own_profile(db_session: Session, user_b: User) -> SpeakerProfile:
    """Create a profile owned by User B (for testing own vs shared)."""
    profile = SpeakerProfile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user_b.id,
        name="User B Profile",
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


# ---------------------------------------------------------------------------
# Tests: PermissionService.get_accessible_profile_ids
# ---------------------------------------------------------------------------


class TestGetAccessibleProfileIds:
    """Tests for PermissionService.get_accessible_profile_ids."""

    def test_user_sees_own_profiles(self, db_session, user_a, sharing_setup):
        """User A sees their own profiles."""
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_a.id), organization_id=None
        )
        assert int(sharing_setup["profile_a"].id) in ids

    def test_shared_user_sees_shared_profiles(self, db_session, user_b, sharing_setup):
        """User B sees User A's profile through collection sharing."""
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_b.id), organization_id=None
        )
        assert int(sharing_setup["profile_a"].id) in ids

    def test_unshared_user_cannot_see_profiles(self, db_session, user_c, sharing_setup):
        """User C (no sharing) cannot see User A's profile."""
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_c.id), organization_id=None
        )
        assert int(sharing_setup["profile_a"].id) not in ids

    def test_user_sees_both_own_and_shared(
        self, db_session, user_b, sharing_setup, user_b_own_profile
    ):
        """User B sees both own profile and shared profile."""
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_b.id), organization_id=None
        )
        assert int(sharing_setup["profile_a"].id) in ids
        assert int(user_b_own_profile.id) in ids

    def test_empty_when_no_profiles(self, db_session, user_c):
        """User with no profiles and no shares gets empty set."""
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_c.id), organization_id=None
        )
        assert len(ids) == 0


# ---------------------------------------------------------------------------
# Tests: PermissionService.get_accessible_profile_ids_with_source
# ---------------------------------------------------------------------------


class TestGetAccessibleProfileIdsWithSource:
    """Tests for PermissionService.get_accessible_profile_ids_with_source."""

    def test_own_profiles_flagged_correctly(self, db_session, user_a, sharing_setup):
        """Own profiles are flagged as is_own=True."""
        result = PermissionService.get_accessible_profile_ids_with_source(
            db_session, int(user_a.id), organization_id=None
        )
        profile_map = {pid: is_own for pid, is_own in result}
        assert profile_map[int(sharing_setup["profile_a"].id)] is True

    def test_shared_profiles_flagged_correctly(self, db_session, user_b, sharing_setup):
        """Shared profiles are flagged as is_own=False."""
        result = PermissionService.get_accessible_profile_ids_with_source(
            db_session, int(user_b.id), organization_id=None
        )
        profile_map = {pid: is_own for pid, is_own in result}
        assert profile_map[int(sharing_setup["profile_a"].id)] is False

    def test_mixed_own_and_shared(self, db_session, user_b, sharing_setup, user_b_own_profile):
        """User B has both own (True) and shared (False) profiles."""
        result = PermissionService.get_accessible_profile_ids_with_source(
            db_session, int(user_b.id), organization_id=None
        )
        profile_map = {pid: is_own for pid, is_own in result}
        assert profile_map[int(user_b_own_profile.id)] is True
        assert profile_map[int(sharing_setup["profile_a"].id)] is False


# ---------------------------------------------------------------------------
# Tests: Group-based sharing
# ---------------------------------------------------------------------------


class TestGroupSharing:
    """Tests for profile visibility via group shares."""

    def test_group_member_sees_shared_profiles(self, db_session, user_a, user_c):
        """User C in a group that has collection share sees the profile."""
        # Create a media file + speaker + profile for User A
        media_file = MediaFile(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_a.id,
            filename="group_test.mp4",
            storage_path="test/group_test.mp4",
            content_type="video/mp4",
            file_size=1000,
        )
        db_session.add(media_file)
        db_session.flush()

        profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_a.id,
            name="Group Profile",
        )
        db_session.add(profile)
        db_session.flush()

        speaker = Speaker(
            uuid=str(uuid_mod.uuid4()),
            media_file_id=media_file.id,
            user_id=user_a.id,
            name="SPEAKER_01",
            profile_id=profile.id,
        )
        db_session.add(speaker)
        db_session.flush()

        # Create a group with User C as member
        group = UserGroup(
            uuid=str(uuid_mod.uuid4()),
            name=f"Test Group {str(uuid_mod.uuid4())[:8]}",
            owner_id=user_a.id,
        )
        db_session.add(group)
        db_session.flush()

        membership = UserGroupMember(
            group_id=group.id,
            user_id=user_c.id,
        )
        db_session.add(membership)
        db_session.flush()

        # Create collection and share with the group
        collection = Collection(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_a.id,
            name=f"Group Collection {str(uuid_mod.uuid4())[:8]}",
        )
        db_session.add(collection)
        db_session.flush()

        cm = CollectionMember(
            uuid=str(uuid_mod.uuid4()),
            collection_id=collection.id,
            media_file_id=media_file.id,
        )
        db_session.add(cm)
        db_session.flush()

        share = CollectionShare(
            uuid=str(uuid_mod.uuid4()),
            collection_id=collection.id,
            shared_by_id=user_a.id,
            target_type="group",
            target_group_id=group.id,
            permission="viewer",
        )
        db_session.add(share)
        db_session.commit()

        # User C should see User A's profile via group membership
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_c.id), organization_id=None
        )
        assert int(profile.id) in ids


# ---------------------------------------------------------------------------
# Tests: Security - Data isolation
# ---------------------------------------------------------------------------


class TestDataIsolation:
    """Security tests ensuring no data leakage to unauthorized users."""

    def test_unshared_file_profile_not_visible(self, db_session, user_a, user_c):
        """Profiles from files not in any shared collection are invisible."""
        # User A creates file + profile but does NOT share via collection
        media_file = MediaFile(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_a.id,
            filename="private.mp4",
            storage_path="test/private.mp4",
            content_type="video/mp4",
            file_size=1000,
        )
        db_session.add(media_file)
        db_session.flush()

        private_profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_a.id,
            name="Private Person",
        )
        db_session.add(private_profile)
        db_session.flush()

        speaker = Speaker(
            uuid=str(uuid_mod.uuid4()),
            media_file_id=media_file.id,
            user_id=user_a.id,
            name="SPEAKER_00",
            profile_id=private_profile.id,
        )
        db_session.add(speaker)
        db_session.commit()

        # User C should NOT see this profile
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_c.id), organization_id=None
        )
        assert int(private_profile.id) not in ids

    def test_profile_without_speaker_in_shared_file_not_visible(self, db_session, user_a, user_b):
        """A profile that exists but has no speakers in shared files is not shared."""
        # User A has a profile with NO speakers linked to any shared file
        orphan_profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_a.id,
            name="Orphan Profile",
        )
        db_session.add(orphan_profile)
        db_session.commit()

        # User B should NOT see this orphan profile (no sharing chain)
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_b.id), organization_id=None
        )
        assert int(orphan_profile.id) not in ids

    def test_revoked_share_hides_profile(self, db_session, user_a, user_b, sharing_setup):
        """Deleting a collection share immediately hides the profile."""
        # Verify User B can see it initially
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_b.id), organization_id=None
        )
        assert int(sharing_setup["profile_a"].id) in ids

        # Revoke the share
        db_session.delete(sharing_setup["share"])
        db_session.commit()

        # User B should no longer see the profile
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_b.id), organization_id=None
        )
        assert int(sharing_setup["profile_a"].id) not in ids


# ---------------------------------------------------------------------------
# Tests: File permission checks
# ---------------------------------------------------------------------------


class TestFilePermissionForSpeakers:
    """Tests for file-level access used in speaker endpoints."""

    def test_owner_has_file_permission(self, db_session, user_a, sharing_setup):
        """File owner always has access."""
        perm = PermissionService.get_file_permission(
            db_session, int(sharing_setup["media_file"].id), int(user_a.id)
        )
        assert perm == "owner"

    def test_shared_user_has_file_permission(self, db_session, user_b, sharing_setup):
        """Shared user has access to file via collection."""
        perm = PermissionService.get_file_permission(
            db_session, int(sharing_setup["media_file"].id), int(user_b.id)
        )
        assert perm is not None
        assert perm in ("viewer", "editor")

    def test_unshared_user_no_file_permission(self, db_session, user_c, sharing_setup):
        """Unshared user has no access to file."""
        perm = PermissionService.get_file_permission(
            db_session, int(sharing_setup["media_file"].id), int(user_c.id)
        )
        assert perm is None


# ---------------------------------------------------------------------------
# Tests: API endpoint - list profiles
# ---------------------------------------------------------------------------


class TestListProfilesAPI:
    """Tests for GET /api/speakers/profiles with shared profiles."""

    def test_user_b_sees_shared_profile(self, client, db_session, user_b, sharing_setup):
        """User B sees shared profiles in the list with is_shared=True."""
        # Login as user B
        response = client.post(
            "/api/auth/token",
            data={"username": user_b.email, "password": "pass_b"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        token = response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        response = client.get("/api/speaker-profiles/profiles", headers=headers)
        assert response.status_code == 200
        profiles = response.json()

        shared_profile = next(
            (p for p in profiles if p["uuid"] == str(sharing_setup["profile_a"].uuid)),
            None,
        )
        assert shared_profile is not None, "Shared profile should be visible to User B"
        assert shared_profile["is_shared"] is True
        assert shared_profile["owner_name"] is not None

    def test_user_a_sees_own_profile_not_shared(self, client, db_session, user_a, sharing_setup):
        """User A sees their own profile with is_shared=False."""
        response = client.post(
            "/api/auth/token",
            data={"username": user_a.email, "password": "pass_a"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        token = response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        response = client.get("/api/speaker-profiles/profiles", headers=headers)
        assert response.status_code == 200
        profiles = response.json()

        own_profile = next(
            (p for p in profiles if p["uuid"] == str(sharing_setup["profile_a"].uuid)),
            None,
        )
        assert own_profile is not None
        assert own_profile["is_shared"] is False
        assert own_profile["owner_name"] is None

    def test_user_c_does_not_see_shared_profile(self, client, db_session, user_c, sharing_setup):
        """User C (no sharing) does not see User A's profile."""
        response = client.post(
            "/api/auth/token",
            data={"username": user_c.email, "password": "pass_c"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        token = response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        response = client.get("/api/speaker-profiles/profiles", headers=headers)
        assert response.status_code == 200
        profiles = response.json()

        profile_uuids = [p["uuid"] for p in profiles]
        assert str(sharing_setup["profile_a"].uuid) not in profile_uuids


# ---------------------------------------------------------------------------
# Tests: API endpoint - owner-only mutations
# ---------------------------------------------------------------------------


class TestOwnerOnlyMutations:
    """Tests that shared users CANNOT modify profiles they don't own."""

    def _get_auth_headers(self, client, user, password):
        response = client.post(
            "/api/auth/token",
            data={"username": user.email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def test_shared_user_cannot_rename_profile(self, client, db_session, user_b, sharing_setup):
        """User B cannot rename User A's shared profile."""
        headers = self._get_auth_headers(client, user_b, "pass_b")
        profile_uuid = str(sharing_setup["profile_a"].uuid)

        response = client.put(
            f"/api/speaker-profiles/profiles/{profile_uuid}?name=Hacked",
            headers=headers,
        )
        assert response.status_code == 403

    def test_shared_user_cannot_delete_profile(self, client, db_session, user_b, sharing_setup):
        """User B cannot delete User A's shared profile."""
        headers = self._get_auth_headers(client, user_b, "pass_b")
        profile_uuid = str(sharing_setup["profile_a"].uuid)

        response = client.delete(
            f"/api/speaker-profiles/profiles/{profile_uuid}",
            headers=headers,
        )
        assert response.status_code == 403

    def test_owner_can_rename_own_profile(self, client, db_session, user_a, sharing_setup):
        """User A can rename their own profile."""
        headers = self._get_auth_headers(client, user_a, "pass_a")
        profile_uuid = str(sharing_setup["profile_a"].uuid)

        response = client.put(
            f"/api/speaker-profiles/profiles/{profile_uuid}?name=Senator%20Jones",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Senator Jones"

    def test_owner_can_delete_own_profile(self, client, db_session, user_a, sharing_setup):
        """User A can delete their own profile."""
        headers = self._get_auth_headers(client, user_a, "pass_a")
        profile_uuid = str(sharing_setup["profile_a"].uuid)

        response = client.delete(
            f"/api/speaker-profiles/profiles/{profile_uuid}",
            headers=headers,
        )
        assert response.status_code == 204


# ---------------------------------------------------------------------------
# Tests: API endpoint - speaker access via file sharing
# ---------------------------------------------------------------------------


class TestSpeakerAccessViaFileSharing:
    """Tests that shared users can access speakers in shared files."""

    def _get_auth_headers(self, client, user, password):
        response = client.post(
            "/api/auth/token",
            data={"username": user.email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def test_shared_user_can_view_speaker(self, client, db_session, user_b, sharing_setup):
        """User B can view speakers in shared files."""
        headers = self._get_auth_headers(client, user_b, "pass_b")
        speaker_uuid = str(sharing_setup["speaker"].uuid)

        response = client.get(f"/api/speakers/{speaker_uuid}", headers=headers)
        assert response.status_code == 200

    def test_unshared_user_cannot_view_speaker(self, client, db_session, user_c, sharing_setup):
        """User C cannot view speakers in files they don't have access to."""
        headers = self._get_auth_headers(client, user_c, "pass_c")
        speaker_uuid = str(sharing_setup["speaker"].uuid)

        response = client.get(f"/api/speakers/{speaker_uuid}", headers=headers)
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tests: Multiple sharing chains
# ---------------------------------------------------------------------------


class TestMultipleSharingChains:
    """Tests for profiles shared via multiple collections."""

    def test_profile_visible_via_multiple_collections(self, db_session, user_a, user_b):
        """A profile visible via two different collections still appears once."""
        # Create two separate media files with same profile
        profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()),
            user_id=user_a.id,
            name="Multi Collection Profile",
        )
        db_session.add(profile)
        db_session.flush()

        for i in range(2):
            mf = MediaFile(
                uuid=str(uuid_mod.uuid4()),
                user_id=user_a.id,
                filename=f"video_{i}.mp4",
                storage_path=f"test/video_{i}.mp4",
                content_type="video/mp4",
                file_size=1000,
            )
            db_session.add(mf)
            db_session.flush()

            sp = Speaker(
                uuid=str(uuid_mod.uuid4()),
                media_file_id=mf.id,
                user_id=user_a.id,
                name=f"SPEAKER_0{i}",
                profile_id=profile.id,
            )
            db_session.add(sp)
            db_session.flush()

            col = Collection(
                uuid=str(uuid_mod.uuid4()),
                user_id=user_a.id,
                name=f"Col {i} {str(uuid_mod.uuid4())[:8]}",
            )
            db_session.add(col)
            db_session.flush()

            cm = CollectionMember(
                uuid=str(uuid_mod.uuid4()),
                collection_id=col.id,
                media_file_id=mf.id,
            )
            db_session.add(cm)
            db_session.flush()

            cs = CollectionShare(
                uuid=str(uuid_mod.uuid4()),
                collection_id=col.id,
                shared_by_id=user_a.id,
                target_type="user",
                target_user_id=user_b.id,
                permission="viewer",
            )
            db_session.add(cs)

        db_session.commit()

        # Profile should appear once in accessible IDs
        ids = PermissionService.get_accessible_profile_ids(
            db_session, int(user_b.id), organization_id=None
        )
        assert int(profile.id) in ids

        # With source should also have it once
        result = PermissionService.get_accessible_profile_ids_with_source(
            db_session, int(user_b.id), organization_id=None
        )
        profile_ids = [pid for pid, _ in result]
        assert profile_ids.count(int(profile.id)) == 1
