"""
Tests for secure media streaming endpoints.

These tests verify that:
1. The stream-url endpoint requires authentication
2. The stream-url endpoint returns proper presigned URLs
3. Direct video/thumbnail endpoints require auth for private files
4. Admin users can access any file
5. Public files can be accessed without auth (if implemented)
"""

import uuid

import pytest
from fastapi import status


class TestStreamUrlEndpoint:
    """Tests for GET /files/{file_uuid}/stream-url"""

    def test_stream_url_requires_authentication(self, client):
        """Unauthenticated requests should be rejected."""
        response = client.get("/api/files/some-uuid/stream-url")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_stream_url_returns_presigned_url(self, client, auth_headers, test_media_file):
        """Authenticated requests should return a presigned URL."""
        response = client.get(
            f"/api/files/{test_media_file.uuid}/stream-url",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert "url" in data
        assert "expires_in" in data
        assert "content_type" in data
        assert data["expires_in"] > 0

    def test_stream_url_video_type(self, client, auth_headers, test_media_file):
        """Video media type should return video URL."""
        response = client.get(
            f"/api/files/{test_media_file.uuid}/stream-url?media_type=video",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert data["content_type"].startswith(("video/", "audio/", "application/"))

    def test_stream_url_thumbnail_type(self, client, auth_headers, test_media_file_with_thumbnail):
        """Thumbnail media type should return thumbnail URL."""
        response = client.get(
            f"/api/files/{test_media_file_with_thumbnail.uuid}/stream-url?media_type=thumbnail",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert data["content_type"] in ("image/jpeg", "image/webp")

    def test_stream_url_invalid_media_type(self, client, auth_headers, test_media_file):
        """Invalid media type should return 400."""
        response = client.get(
            f"/api/files/{test_media_file.uuid}/stream-url?media_type=invalid",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_stream_url_access_denied_wrong_user(
        self, client, other_user_auth_headers, test_media_file
    ):
        """Users should not access other users' files."""
        response = client.get(
            f"/api/files/{test_media_file.uuid}/stream-url",
            headers=other_user_auth_headers,
        )
        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        )

    def test_stream_url_admin_access_any_file(self, client, admin_auth_headers, test_media_file):
        """Admin users should be able to access any file."""
        response = client.get(
            f"/api/files/{test_media_file.uuid}/stream-url",
            headers=admin_auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK


class TestRemovedLegacyEndpoints:
    """The byte-proxy endpoints were removed in favor of presigned URLs.

    Media now streams directly from MinIO via /stream-url (playback) and
    prepare-download (downloads), so these routes must no longer exist.
    """

    @pytest.mark.parametrize(
        "suffix",
        ["video", "simple-video", "content", "download", "download-with-token"],
    )
    def test_legacy_byte_proxy_routes_are_gone(self, client, auth_headers, test_media_file, suffix):
        """Removed routes should 404 (route no longer registered), even when authed."""
        response = client.get(
            f"/api/files/{test_media_file.uuid}/{suffix}",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDirectThumbnailEndpoint:
    """Tests for GET /files/{file_uuid}/thumbnail"""

    def test_thumbnail_requires_auth_for_private_files(
        self, client, test_media_file_with_thumbnail
    ):
        """Private files should require authentication for thumbnails."""
        response = client.get(f"/api/files/{test_media_file_with_thumbnail.uuid}/thumbnail")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_thumbnail_with_auth_succeeds(
        self, client, auth_headers, test_media_file_with_thumbnail
    ):
        """Authenticated users can access thumbnails of their files."""
        response = client.get(
            f"/api/files/{test_media_file_with_thumbnail.uuid}/thumbnail",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK


class TestCacheControlHeaders:
    """Tests for Cache-Control security headers on the retained thumbnail fallback."""

    def test_private_thumbnail_has_no_store_header(
        self, client, auth_headers, test_media_file_with_thumbnail
    ):
        """Private files' thumbnails should have Cache-Control: private, no-store."""
        response = client.get(
            f"/api/files/{test_media_file_with_thumbnail.uuid}/thumbnail",
            headers=auth_headers,
        )

        cache_control = response.headers.get("cache-control", "")
        # Private files should not be cached by shared caches
        assert "private" in cache_control or "no-store" in cache_control


# Fixtures - these would typically be in conftest.py
@pytest.fixture
def test_media_file(db_session, test_user):
    """Create a test media file for the test user."""
    from app.models.media import MediaFile

    file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="test_video.mp4",
        storage_path="media/test/test_video.mp4",
        content_type="video/mp4",
        file_size=1024000,
        user_id=test_user.id,
        status="completed",
        is_public=False,
    )
    db_session.add(file)
    db_session.commit()
    db_session.refresh(file)
    return file


@pytest.fixture
def test_media_file_with_thumbnail(db_session, test_user):
    """Create a test media file with a real thumbnail object in storage.

    When MinIO is reachable (SKIP_S3=False) the thumbnail bytes are uploaded so
    the 200 path is genuinely exercised; the object is removed on teardown.
    The path is UUID-unique so parallel test workers never collide.
    """
    import io
    import os

    from app.models.media import MediaFile

    file_uuid = str(uuid.uuid4())
    thumbnail_path = f"media/test/thumbs/{file_uuid}.webp"
    s3_live = os.environ.get("SKIP_S3", "True").lower() != "true"

    if s3_live:
        from app.core.config import settings
        from app.services.minio_service import minio_client

        thumb_bytes = b"RIFF\x1a\x00\x00\x00WEBPVP8 "  # minimal WebP-flavoured payload
        minio_client.put_object(
            settings.MEDIA_BUCKET_NAME,
            thumbnail_path,
            io.BytesIO(thumb_bytes),
            length=len(thumb_bytes),
            content_type="image/webp",
        )

    file = MediaFile(
        uuid=file_uuid,
        filename="test_video_thumb.mp4",
        storage_path=f"media/test/{file_uuid}.mp4",
        thumbnail_path=thumbnail_path,
        content_type="video/mp4",
        file_size=1024000,
        user_id=test_user.id,
        status="completed",
        is_public=False,
    )
    db_session.add(file)
    db_session.commit()
    db_session.refresh(file)

    yield file

    if s3_live:
        try:
            minio_client.remove_object(settings.MEDIA_BUCKET_NAME, thumbnail_path)
        except Exception:
            pass  # best-effort cleanup of the test object
