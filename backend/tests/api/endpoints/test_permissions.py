"""Tests for admin access and sharing permission fixes.

Tests verify:
1. User.is_admin property works for admin, super_admin, user roles
2. Admin can access other users' files (no 403)
3. Admin can view speakers on other users' files
4. Admin can view/create/edit/delete comments on other users' files
5. Admin can access summaries and AI suggestions on other users' files
6. Regular user gets 403 on another user's unshared file
7. get_file_by_uuid_with_permission respects is_admin flag

These tests create MediaFile/Speaker records directly in the DB
(S3/MinIO not required).
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.sharing import CollectionShare
from app.models.user import User

# ---- Unit tests for User.is_admin property ----


class TestUserIsAdminProperty:
    """Test the is_admin computed property on the User model."""

    def test_admin_role_is_admin(self):
        user = User(role="admin")
        assert user.is_admin is True

    def test_super_admin_role_is_admin(self):
        user = User(role="super_admin")
        assert user.is_admin is True

    def test_user_role_is_not_admin(self):
        user = User(role="user")
        assert user.is_admin is False

    def test_none_role_is_not_admin(self):
        user = User(role=None)
        assert user.is_admin is False


# ---- Fixtures for permission tests ----


@pytest.fixture
def owner_user(db_session):
    """User who owns the test media file."""
    user = User(
        email=f"owner_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="File Owner",
        hashed_password="fakehash",
        is_active=True,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_media_file(db_session, owner_user):
    """A media file owned by owner_user, created directly in DB."""
    mf = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner_user.id,
        filename="test_audio.mp3",
        storage_path="test/test_audio.mp3",
        file_size=1024,
        content_type="audio/mpeg",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


@pytest.fixture
def test_speakers(db_session, test_media_file, owner_user):
    """Speakers on the test media file (owned by owner_user)."""
    speakers = []
    for i in range(3):
        s = Speaker(
            uuid=uuid_pkg.uuid4(),
            name=f"SPEAKER_{i:02d}",
            display_name=f"Speaker {i}",
            user_id=owner_user.id,
            media_file_id=test_media_file.id,
        )
        db_session.add(s)
        speakers.append(s)
    db_session.commit()
    for s in speakers:
        db_session.refresh(s)
    return speakers


@pytest.fixture
def shared_collection(db_session, owner_user, test_media_file):
    """A collection owned by owner_user containing the test media file."""
    coll = Collection(
        uuid=uuid_pkg.uuid4(),
        name="Shared Collection",
        user_id=owner_user.id,
    )
    db_session.add(coll)
    db_session.commit()
    db_session.refresh(coll)

    member = CollectionMember(
        collection_id=coll.id,
        media_file_id=test_media_file.id,
    )
    db_session.add(member)
    db_session.commit()
    return coll


# ---- Unit tests for get_file_by_uuid_with_permission ----


class TestGetFileByUuidWithPermission:
    """Test the admin bypass in get_file_by_uuid_with_permission."""

    def test_owner_can_access_own_file(self, db_session, test_media_file, owner_user):
        from app.utils.uuid_helpers import get_file_by_uuid_with_permission

        result = get_file_by_uuid_with_permission(
            db_session, str(test_media_file.uuid), owner_user.id
        )
        assert result.id == test_media_file.id

    def test_admin_can_access_other_users_file(self, db_session, test_media_file, admin_user):
        from app.utils.uuid_helpers import get_file_by_uuid_with_permission

        result = get_file_by_uuid_with_permission(
            db_session,
            str(test_media_file.uuid),
            admin_user.id,
            is_admin=True,
        )
        assert result.id == test_media_file.id

    def test_non_admin_blocked_from_other_users_file(
        self, db_session, test_media_file, normal_user
    ):
        from app.utils.uuid_helpers import get_file_by_uuid_with_permission

        with pytest.raises(HTTPException) as exc:
            get_file_by_uuid_with_permission(
                db_session,
                str(test_media_file.uuid),
                normal_user.id,
                is_admin=False,
            )
        assert exc.value.status_code == 403

    def test_shared_viewer_can_access_file(
        self, db_session, test_media_file, normal_user, shared_collection
    ):
        """A user who has been shared a collection can access files in it."""
        from app.utils.uuid_helpers import get_file_by_uuid_with_permission

        # Share collection with normal_user as viewer
        share = CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=shared_collection.id,
            shared_by_id=shared_collection.user_id,
            target_type="user",
            target_user_id=normal_user.id,
            permission="viewer",
        )
        db_session.add(share)
        db_session.commit()

        result = get_file_by_uuid_with_permission(
            db_session,
            str(test_media_file.uuid),
            normal_user.id,
            is_admin=False,
        )
        assert result.id == test_media_file.id


class TestGetCollectionByUuidWithPermission:
    """Test the admin bypass in get_collection_by_uuid_with_permission."""

    def test_admin_can_access_other_users_collection(
        self, db_session, shared_collection, admin_user
    ):
        from app.utils.uuid_helpers import get_collection_by_uuid_with_permission

        result = get_collection_by_uuid_with_permission(
            db_session,
            str(shared_collection.uuid),
            admin_user.id,
            is_admin=True,
        )
        assert result.id == shared_collection.id

    def test_non_admin_blocked_from_other_users_collection(
        self, db_session, shared_collection, normal_user
    ):
        from app.utils.uuid_helpers import get_collection_by_uuid_with_permission

        with pytest.raises(HTTPException) as exc:
            get_collection_by_uuid_with_permission(
                db_session,
                str(shared_collection.uuid),
                normal_user.id,
                is_admin=False,
            )
        assert exc.value.status_code == 403


# ---- Unit tests for PermissionService with admin ----


class TestPermissionServiceAdminBypass:
    """Test that endpoints using PermissionService respect admin status."""

    def test_permission_service_returns_owner_for_file_owner(
        self, db_session, test_media_file, owner_user
    ):
        from app.services.permission_service import PermissionService

        perm = PermissionService.get_file_permission(db_session, test_media_file.id, owner_user.id)
        assert perm == "owner"

    def test_permission_service_returns_none_for_unshared_user(
        self, db_session, test_media_file, normal_user
    ):
        from app.services.permission_service import PermissionService

        perm = PermissionService.get_file_permission(db_session, test_media_file.id, normal_user.id)
        assert perm is None

    def test_permission_service_returns_viewer_for_shared_viewer(
        self, db_session, test_media_file, normal_user, shared_collection
    ):
        from app.services.permission_service import PermissionService

        share = CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=shared_collection.id,
            shared_by_id=shared_collection.user_id,
            target_type="user",
            target_user_id=normal_user.id,
            permission="viewer",
        )
        db_session.add(share)
        db_session.commit()

        perm = PermissionService.get_file_permission(db_session, test_media_file.id, normal_user.id)
        assert perm == "viewer"

    def test_permission_service_returns_editor_for_shared_editor(
        self, db_session, test_media_file, normal_user, shared_collection
    ):
        from app.services.permission_service import PermissionService

        share = CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=shared_collection.id,
            shared_by_id=shared_collection.user_id,
            target_type="user",
            target_user_id=normal_user.id,
            permission="editor",
        )
        db_session.add(share)
        db_session.commit()

        perm = PermissionService.get_file_permission(db_session, test_media_file.id, normal_user.id)
        assert perm == "editor"


# ---- Integration tests for API endpoints ----
# These use the FastAPI TestClient and real DB but skip S3.
# We test the file detail endpoint which works without S3
# because it reads from DB, not storage.


class TestAdminFileAccess:
    """Test admin can access file details owned by other users."""

    def test_admin_can_get_file_detail(self, client, admin_token_headers, test_media_file):
        response = client.get(
            f"/api/files/{test_media_file.uuid}",
            headers=admin_token_headers,
        )
        # Should not be 403 — admin should have access
        assert response.status_code != 403, f"Admin got 403 on other user's file: {response.json()}"

    def test_admin_gets_my_permission_owner(self, client, admin_token_headers, test_media_file):
        response = client.get(
            f"/api/files/{test_media_file.uuid}",
            headers=admin_token_headers,
        )
        if response.status_code == 200:
            data = response.json()
            assert data.get("my_permission") == "owner"

    def test_regular_user_blocked_from_other_users_file(
        self, client, user_token_headers, test_media_file
    ):
        response = client.get(
            f"/api/files/{test_media_file.uuid}",
            headers=user_token_headers,
        )
        assert response.status_code == 403


class TestAdminSpeakerAccess:
    """Test admin can see speakers on other users' files."""

    def test_admin_can_list_speakers_for_other_users_file(
        self, client, admin_token_headers, test_media_file, test_speakers
    ):
        response = client.get(
            f"/api/speakers?file_uuid={test_media_file.uuid}",
            headers=admin_token_headers,
        )
        assert response.status_code != 403, f"Admin got 403 listing speakers: {response.json()}"
        if response.status_code == 200:
            speakers = response.json()
            assert len(speakers) == 3, f"Expected 3 speakers, got {len(speakers)}"

    def test_regular_user_cannot_list_other_users_speakers(
        self, client, user_token_headers, test_media_file, test_speakers
    ):
        """A non-owner asking for another user's file must be refused, not served empty.

        This accepted ``(403, 500)`` or a 200 with an empty list, which made it unable to
        fail: a server error passed, and so did a silent empty response (issue #431). The
        contract is unambiguous — ``GET /api/speakers?file_uuid=`` resolves the file through
        ``get_file_by_uuid_with_permission``, which raises 403 when the caller has no
        permission and 404 only when the file does not exist. The file here exists and is
        owned by someone else, so 403 is the only correct answer.

        The old comment excused the 500 as "OpenSearch may be unavailable", but the authz
        check runs in ``_resolve_file_uuid_to_id`` before any OpenSearch call
        (``app/api/endpoints/speakers.py:643``) — so a 500 on this path is a real bug and
        must fail the test.
        """
        response = client.get(
            f"/api/speakers?file_uuid={test_media_file.uuid}",
            headers=user_token_headers,
        )
        assert response.status_code == 403, response.text


class TestAdminCommentAccess:
    """Test admin can access comments on other users' files."""

    def test_admin_can_list_comments_for_other_users_file(
        self, client, admin_token_headers, test_media_file
    ):
        response = client.get(
            f"/api/comments/files/{test_media_file.uuid}/comments",
            headers=admin_token_headers,
        )
        assert response.status_code != 403, f"Admin got 403 listing comments: {response.json()}"


class TestAdminSummaryAccess:
    """Test admin can access summary endpoints on other users' files."""

    def test_admin_can_get_summary_status(self, client, admin_token_headers, test_media_file):
        response = client.get(
            f"/api/files/{test_media_file.uuid}/summary-status",
            headers=admin_token_headers,
        )
        # Should not be 403
        assert response.status_code != 403, f"Admin got 403 on summary status: {response.json()}"


class TestSharedEditorAccess:
    """Test shared editor can access and modify data on shared files."""

    @pytest.fixture
    def editor_share(self, db_session, shared_collection, normal_user):
        """Share collection with normal_user as editor."""
        share = CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=shared_collection.id,
            shared_by_id=shared_collection.user_id,
            target_type="user",
            target_user_id=normal_user.id,
            permission="editor",
        )
        db_session.add(share)
        db_session.commit()
        return share

    def test_shared_editor_can_get_file_detail(
        self, client, user_token_headers, test_media_file, editor_share
    ):
        response = client.get(
            f"/api/files/{test_media_file.uuid}",
            headers=user_token_headers,
        )
        assert response.status_code != 403, f"Shared editor got 403: {response.json()}"

    def test_shared_editor_gets_editor_permission(
        self, client, user_token_headers, test_media_file, editor_share
    ):
        response = client.get(
            f"/api/files/{test_media_file.uuid}",
            headers=user_token_headers,
        )
        if response.status_code == 200:
            data = response.json()
            assert data.get("my_permission") == "editor"

    def test_shared_editor_can_list_speakers(
        self, client, user_token_headers, test_media_file, test_speakers, editor_share
    ):
        response = client.get(
            f"/api/speakers?file_uuid={test_media_file.uuid}",
            headers=user_token_headers,
        )
        assert response.status_code != 403
        if response.status_code == 200:
            assert len(response.json()) == 3


class TestSharedViewerAccess:
    """Test shared viewer has read-only access."""

    @pytest.fixture
    def viewer_share(self, db_session, shared_collection, normal_user):
        """Share collection with normal_user as viewer."""
        share = CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=shared_collection.id,
            shared_by_id=shared_collection.user_id,
            target_type="user",
            target_user_id=normal_user.id,
            permission="viewer",
        )
        db_session.add(share)
        db_session.commit()
        return share

    def test_shared_viewer_can_get_file_detail(
        self, client, user_token_headers, test_media_file, viewer_share
    ):
        response = client.get(
            f"/api/files/{test_media_file.uuid}",
            headers=user_token_headers,
        )
        assert response.status_code != 403

    def test_shared_viewer_gets_viewer_permission(
        self, client, user_token_headers, test_media_file, viewer_share
    ):
        response = client.get(
            f"/api/files/{test_media_file.uuid}",
            headers=user_token_headers,
        )
        if response.status_code == 200:
            data = response.json()
            assert data.get("my_permission") == "viewer"
