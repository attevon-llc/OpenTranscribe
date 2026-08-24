"""Unit tests for the MediacmsProvider plugin.

Tests cover URL handling, credential resolution (own > shared > system > env),
SSL per-host settings, token/URL parsing, media info building, and the
login + download flow (mocked HTTP).
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.services.protected_media_plugins.mediacms import MediacmsProvider
from tests.helpers import stub_public_dns

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _public_media_host_dns(monkeypatch):
    """Let ``*.example.com`` resolve to a public address for this module.

    Every outbound request the provider makes is now re-checked against
    ``utils/url_validation`` immediately before it is issued (audit finding A1), so the
    ``m.example.com`` hosts these tests use must resolve to something public or the
    guard refuses them for a reason unrelated to what is being tested.

    The guard's own behaviour is covered in ``unit/test_media_source_ssrf.py``, which
    does **not** stub DNS for its hostile hosts.
    """
    stub_public_dns(monkeypatch)


@pytest.fixture
def provider():
    """Fresh MediacmsProvider instance for each test."""
    return MediacmsProvider()


@pytest.fixture
def _clear_mediacms_env(monkeypatch):
    """Ensure MediaCMS env vars are cleared for clean tests."""
    monkeypatch.delenv("MEDIACMS_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MEDIACMS_VERIFY_SSL", raising=False)


# Helper: build a mock UserMediaSource row for _get_user_media_sources
def _mock_user_source(
    hostname: str,
    *,
    user_id: int = 1,
    username: str = "u",
    password: str = "p",
    verify_ssl: bool = True,
    shared: bool = False,
    label: str = "",
):
    src = MagicMock()
    src.hostname = hostname
    src.provider_type = "mediacms"
    src.username = username
    src.password = password  # already "encrypted" in mock
    src.verify_ssl = verify_ssl
    src.label = label
    src.user_id = user_id
    src.is_active = True
    src.is_shared = shared
    return src


# ===================================================================
# can_handle
# ===================================================================


class TestCanHandle:
    """MediacmsProvider.can_handle() URL matching."""

    def test_query_param_url(self, provider, _clear_mediacms_env):
        """URLs with ?m=<token> on allowed hosts are handled."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            assert provider.can_handle("https://media.example.com/view?m=abc123") is True

    def test_api_path_url(self, provider, _clear_mediacms_env):
        """URLs with /api/v1/media/<token> on allowed hosts are handled."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            assert provider.can_handle("https://media.example.com/api/v1/media/abc123") is True

    def test_unknown_host_rejected(self, provider, _clear_mediacms_env):
        """URLs from unknown hosts are not handled."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            assert provider.can_handle("https://other.example.com/view?m=abc") is False

    def test_no_token_rejected(self, provider, _clear_mediacms_env):
        """URLs without a media token are not handled."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            assert provider.can_handle("https://media.example.com/about") is False

    def test_non_http_rejected(self, provider, _clear_mediacms_env):
        """Non-HTTP(S) schemes are rejected."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            assert provider.can_handle("ftp://media.example.com/view?m=abc") is False

    def test_can_handle_with_user_id(self, provider, _clear_mediacms_env):
        """can_handle passes user_id through to _get_allowed_hosts."""
        with patch.object(
            provider, "_get_allowed_hosts", return_value={"user-media.example.com"}
        ) as mock_hosts:
            result = provider.can_handle("https://user-media.example.com/view?m=tok", user_id=42)
            assert result is True
            mock_hosts.assert_called_once_with(42)

    def test_malformed_url_returns_false(self, provider, _clear_mediacms_env):
        """Badly formed URLs should return False, not raise."""
        with patch.object(provider, "_get_allowed_hosts", return_value=set()):
            assert provider.can_handle("not a url at all") is False


# ===================================================================
# Allowed Hosts Aggregation
# ===================================================================


class TestAllowedHosts:
    """Host aggregation from per-user, system, and env sources."""

    def test_env_hosts_included(self, provider, monkeypatch):
        """MEDIACMS_ALLOWED_HOSTS env var contributes to allowed hosts."""
        monkeypatch.setenv("MEDIACMS_ALLOWED_HOSTS", "env1.example.com,env2.example.com")
        with (
            patch.object(provider, "_get_user_media_sources", return_value=[]),
            patch.object(provider, "_get_db_sources", return_value=[]),
        ):
            hosts = provider._get_allowed_hosts()
            assert "env1.example.com" in hosts
            assert "env2.example.com" in hosts

    def test_user_sources_included(self, provider, _clear_mediacms_env):
        """Per-user media sources contribute to allowed hosts."""
        user_sources = [{"hostname": "user-host.example.com", "provider_type": "mediacms"}]
        with (
            patch.object(provider, "_get_user_media_sources", return_value=user_sources),
            patch.object(provider, "_get_db_sources", return_value=[]),
        ):
            hosts = provider._get_allowed_hosts(user_id=1)
            assert "user-host.example.com" in hosts

    def test_system_sources_included(self, provider, _clear_mediacms_env):
        """System-level (legacy admin) sources contribute to allowed hosts."""
        system_sources = [{"hostname": "system-host.example.com", "provider_type": "mediacms"}]
        with (
            patch.object(provider, "_get_user_media_sources", return_value=[]),
            patch.object(provider, "_get_db_sources", return_value=system_sources),
        ):
            hosts = provider._get_allowed_hosts()
            assert "system-host.example.com" in hosts

    def test_all_sources_merged(self, provider, monkeypatch):
        """Hosts from all sources (user, system, env) are merged."""
        monkeypatch.setenv("MEDIACMS_ALLOWED_HOSTS", "env.example.com")
        user_sources = [{"hostname": "user.example.com"}]
        system_sources = [{"hostname": "system.example.com"}]
        with (
            patch.object(provider, "_get_user_media_sources", return_value=user_sources),
            patch.object(provider, "_get_db_sources", return_value=system_sources),
        ):
            hosts = provider._get_allowed_hosts(user_id=1)
            assert hosts == {"env.example.com", "user.example.com", "system.example.com"}

    def test_empty_hosts_stripped(self, provider, monkeypatch):
        """Empty/whitespace env entries should be ignored."""
        monkeypatch.setenv("MEDIACMS_ALLOWED_HOSTS", " , ,valid.example.com, ")
        with (
            patch.object(provider, "_get_user_media_sources", return_value=[]),
            patch.object(provider, "_get_db_sources", return_value=[]),
        ):
            hosts = provider._get_allowed_hosts()
            assert hosts == {"valid.example.com"}


# ===================================================================
# Credential Resolution Priority
# ===================================================================


class TestCredentialResolution:
    """_get_stored_credentials priority: own > shared > system > env."""

    def test_own_credentials_take_priority(self, provider, _clear_mediacms_env):
        """User's own credentials are returned first."""
        sources = [
            {
                "hostname": "h.example.com",
                "username": "own-user",
                "password": "own-pass",
                "user_id": 1,
            },
            {
                "hostname": "h.example.com",
                "username": "shared-user",
                "password": "shared-pass",
                "user_id": 2,
            },
        ]
        with patch.object(provider, "_get_all_sources", return_value=sources):
            u, p = provider._get_stored_credentials("h.example.com", user_id=1)
            assert u == "own-user"
            assert p == "own-pass"

    def test_shared_fallback_when_no_own(self, provider, _clear_mediacms_env):
        """Shared credentials used when user has no own source for the host."""
        sources = [
            {
                "hostname": "h.example.com",
                "username": "shared-user",
                "password": "shared-pass",
                "user_id": 2,
            },
        ]
        with patch.object(provider, "_get_all_sources", return_value=sources):
            u, p = provider._get_stored_credentials("h.example.com", user_id=1)
            assert u == "shared-user"
            assert p == "shared-pass"

    def test_no_credentials_returns_none(self, provider, _clear_mediacms_env):
        """Returns (None, None) when no sources match."""
        with patch.object(provider, "_get_all_sources", return_value=[]):
            u, p = provider._get_stored_credentials("unknown.example.com")
            assert u is None
            assert p is None

    def test_partial_credentials_skipped(self, provider, _clear_mediacms_env):
        """Sources with only username (no password) are skipped."""
        sources = [
            {"hostname": "h.example.com", "username": "partial", "password": ""},
            {"hostname": "h.example.com", "username": "full", "password": "pass"},
        ]
        with patch.object(provider, "_get_all_sources", return_value=sources):
            u, p = provider._get_stored_credentials("h.example.com")
            assert u == "full"
            assert p == "pass"


# ===================================================================
# SSL Verification Per-Host
# ===================================================================


class TestSSLVerification:
    """Per-host SSL verification settings."""

    def test_host_specific_ssl_false(self, provider, _clear_mediacms_env):
        """When a source has verify_ssl=False, that setting is returned."""
        sources = [{"hostname": "nossl.example.com", "verify_ssl": False}]
        with patch.object(provider, "_get_all_sources", return_value=sources):
            assert provider._get_verify_ssl_for_host("nossl.example.com") is False

    def test_host_specific_ssl_true(self, provider, _clear_mediacms_env):
        """When a source has verify_ssl=True, that setting is returned."""
        sources = [{"hostname": "ssl.example.com", "verify_ssl": True}]
        with patch.object(provider, "_get_all_sources", return_value=sources):
            assert provider._get_verify_ssl_for_host("ssl.example.com") is True

    def test_unknown_host_falls_back_to_env(self, provider, monkeypatch):
        """Unknown host uses MEDIACMS_VERIFY_SSL env var."""
        monkeypatch.setenv("MEDIACMS_VERIFY_SSL", "false")
        with patch.object(provider, "_get_all_sources", return_value=[]):
            assert provider._get_verify_ssl_for_host("unknown.example.com") is False

    def test_unknown_host_defaults_true(self, provider, _clear_mediacms_env):
        """Without env or DB config, default is True."""
        with patch.object(provider, "_get_all_sources", return_value=[]):
            assert provider._get_verify_ssl_for_host("unknown.example.com") is True


# ===================================================================
# Token & URL Parsing
# ===================================================================


class TestTokenAndBaseURL:
    """_get_token_and_base_url parsing."""

    def test_query_param_token(self, provider):
        """Extracts token from ?m=<token>."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            token, base = provider._get_token_and_base_url(
                "https://media.example.com/view?m=abc123"
            )
            assert token == "abc123"
            assert base == "https://media.example.com"

    def test_api_path_token(self, provider):
        """Extracts token from /api/v1/media/<token>."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            token, base = provider._get_token_and_base_url(
                "https://media.example.com/api/v1/media/xyz789"
            )
            assert token == "xyz789"
            assert base == "https://media.example.com"

    def test_host_not_allowed_raises(self, provider):
        """Raises HTTPException if hostname is not allowed."""
        with patch.object(provider, "_get_allowed_hosts", return_value=set()):
            with pytest.raises(HTTPException) as exc:
                provider._get_token_and_base_url("https://evil.example.com/view?m=abc")
            assert exc.value.status_code == 400

    def test_missing_token_raises(self, provider):
        """Raises HTTPException if no token can be extracted."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            with pytest.raises(HTTPException) as exc:
                provider._get_token_and_base_url("https://media.example.com/about")
            assert exc.value.status_code == 400
            assert "Missing media token" in str(exc.value.detail)

    def test_preserves_scheme(self, provider):
        """HTTP scheme is preserved in base_url (not forced to HTTPS)."""
        with patch.object(provider, "_get_allowed_hosts", return_value={"media.example.com"}):
            _, base = provider._get_token_and_base_url("http://media.example.com/view?m=tok")
            assert base.startswith("http://")


# ===================================================================
# Build Media Info
# ===================================================================


class TestBuildMediaInfo:
    """_build_media_info constructs a yt-dlp-compatible dict."""

    def test_basic_fields(self, provider):
        """Core fields are populated from API response."""
        info = provider._build_media_info(
            "token123",
            "https://media.example.com",
            {
                "title": "My Video",
                "description": "A test video",
                "owner": "alice",
                "duration": 120.5,
            },
            "https://media.example.com/view?m=token123",
        )
        assert info["id"] == "token123"
        assert info["title"] == "My Video"
        assert info["description"] == "A test video"
        assert info["uploader"] == "alice"
        assert info["duration"] == 120.5
        assert info["extractor"] == "mediacms"
        assert info["source"] == "mediacms"

    def test_relative_thumbnail_normalized(self, provider):
        """Relative thumbnail paths are converted to absolute URLs."""
        info = provider._build_media_info(
            "tok",
            "https://media.example.com",
            {"thumbnail_url": "/media/original/thumbnails/thumb.jpg"},
            "https://media.example.com/view?m=tok",
        )
        assert info["thumbnail"] == "https://media.example.com/media/original/thumbnails/thumb.jpg"

    def test_absolute_thumbnail_preserved(self, provider):
        """Absolute thumbnail URLs are preserved as-is."""
        info = provider._build_media_info(
            "tok",
            "https://media.example.com",
            {"thumbnail_url": "https://cdn.example.com/thumb.jpg"},
            "https://media.example.com/view?m=tok",
        )
        assert info["thumbnail"] == "https://cdn.example.com/thumb.jpg"

    def test_missing_thumbnail(self, provider):
        """Missing thumbnail_url produces None."""
        info = provider._build_media_info("tok", "https://m.example.com", {}, "url")
        assert info["thumbnail"] is None

    def test_fallback_title(self, provider):
        """When title is missing, friendly_token is used."""
        info = provider._build_media_info("fallback_tok", "https://m.example.com", {}, "url")
        assert info["title"] == "fallback_tok"

    def test_raw_info_preserved(self, provider):
        """Full MediaCMS API response is stored under mediacms_raw."""
        raw = {"title": "T", "extra_field": "value"}
        info = provider._build_media_info("tok", "https://m.example.com", raw, "url")
        assert info["mediacms_raw"] == raw
        assert info["mediacms_base_url"] == "https://m.example.com"


# ===================================================================
# Login and Get Info (mocked HTTP)
# ===================================================================


class TestLoginAndGetInfo:
    """_login_and_get_info with mocked requests."""

    def _mock_login_success(self, base_url: str, token: str, media_info: dict):
        """Return a side_effect function for requests.post/get mocking."""
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "auth-tok-123"}
        login_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.json.return_value = media_info
        info_resp.raise_for_status = MagicMock()

        return login_resp, info_resp

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_successful_login_and_info(self, mock_requests, provider, _clear_mediacms_env):
        """Successful login returns token, base_url, info, and auth_token."""
        login_resp, info_resp = self._mock_login_success(
            "https://m.example.com",
            "vid123",
            {"title": "Test Video", "duration": 60},
        )
        mock_requests.post.return_value = login_resp
        mock_requests.get.return_value = info_resp

        with (
            patch.object(
                provider,
                "_get_token_and_base_url",
                return_value=("vid123", "https://m.example.com"),
            ),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            token, base, info, auth = provider._login_and_get_info(
                "https://m.example.com/view?m=vid123",
                username="alice",
                password="secret",
            )
            assert token == "vid123"
            assert base == "https://m.example.com"
            assert info["title"] == "Test Video"
            assert auth == "auth-tok-123"

        # Verify login was called with correct credentials
        mock_requests.post.assert_called_once()
        call_kwargs = mock_requests.post.call_args
        assert call_kwargs.kwargs["data"]["username"] == "alice"
        assert call_kwargs.kwargs["data"]["password"] == "secret"

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_falls_back_to_stored_credentials(self, mock_requests, provider, _clear_mediacms_env):
        """When no credentials provided, falls back to stored."""
        login_resp, info_resp = self._mock_login_success(
            "https://m.example.com", "tok", {"title": "V"}
        )
        mock_requests.post.return_value = login_resp
        mock_requests.get.return_value = info_resp

        with (
            patch.object(
                provider, "_get_token_and_base_url", return_value=("tok", "https://m.example.com")
            ),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(
                provider, "_get_stored_credentials", return_value=("stored-user", "stored-pass")
            ),
        ):
            provider._login_and_get_info("https://m.example.com/view?m=tok")

        call_kwargs = mock_requests.post.call_args
        assert call_kwargs.kwargs["data"]["username"] == "stored-user"
        assert call_kwargs.kwargs["data"]["password"] == "stored-pass"

    def test_no_credentials_raises(self, provider, _clear_mediacms_env):
        """Raises 400 when no credentials available anywhere."""
        with (
            patch.object(
                provider, "_get_token_and_base_url", return_value=("tok", "https://m.example.com")
            ),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            with pytest.raises(HTTPException) as exc:
                provider._login_and_get_info("https://m.example.com/view?m=tok")
            assert exc.value.status_code == 400
            assert "Credentials" in str(exc.value.detail)

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_login_no_token_raises(self, mock_requests, provider, _clear_mediacms_env):
        """Raises 502 when login response has no token."""
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {}  # No token
        login_resp.raise_for_status = MagicMock()
        mock_requests.post.return_value = login_resp

        with (
            patch.object(
                provider, "_get_token_and_base_url", return_value=("tok", "https://m.example.com")
            ),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            with pytest.raises(HTTPException) as exc:
                provider._login_and_get_info(
                    "https://m.example.com/view?m=tok", username="u", password="p"
                )
            assert exc.value.status_code == 502
            assert "auth token" in str(exc.value.detail)

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_login_request_error_wraps_as_502(self, mock_requests, provider, _clear_mediacms_env):
        """Network errors during login are wrapped as 502."""
        import requests

        mock_requests.post.side_effect = requests.exceptions.ConnectionError("Connection refused")
        mock_requests.exceptions = requests.exceptions

        with (
            patch.object(
                provider, "_get_token_and_base_url", return_value=("tok", "https://m.example.com")
            ),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            with pytest.raises(HTTPException) as exc:
                provider._login_and_get_info(
                    "https://m.example.com/view?m=tok", username="u", password="p"
                )
            assert exc.value.status_code == 502


# ===================================================================
# extract_info (integration of login + build)
# ===================================================================


class TestExtractInfo:
    """extract_info delegates to _login_and_get_info + _build_media_info."""

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_extract_info_returns_media_dict(self, mock_requests, provider, _clear_mediacms_env):
        """extract_info returns a complete media info dict."""
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "auth-tok"}
        login_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.json.return_value = {"title": "Info Video", "duration": 90}
        info_resp.raise_for_status = MagicMock()

        mock_requests.post.return_value = login_resp
        mock_requests.get.return_value = info_resp

        with (
            patch.object(provider, "_get_allowed_hosts", return_value={"m.example.com"}),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            result = provider.extract_info(
                "https://m.example.com/view?m=vid1",
                username="u",
                password="p",
                user_id=5,
            )
            assert result["title"] == "Info Video"
            assert result["extractor"] == "mediacms"


# ===================================================================
# Download (mocked HTTP)
# ===================================================================


class TestDownload:
    """download() with mocked HTTP for login + file download."""

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_download_success(self, mock_requests, provider, tmp_path, _clear_mediacms_env):
        """Successful download writes file and returns info."""
        # Login mock
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "auth-tok"}
        login_resp.raise_for_status = MagicMock()

        # Info mock
        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.json.return_value = {
            "title": "Download Test",
            "original_media_url": "/media/original/video.mp4",
            "duration": 45,
        }
        info_resp.raise_for_status = MagicMock()

        # Download mock (streaming)
        download_resp = MagicMock()
        download_resp.status_code = 200
        download_resp.headers = {"Content-Length": "1024"}
        download_resp.iter_content.return_value = [b"x" * 1024]
        download_resp.raise_for_status = MagicMock()
        download_resp.__enter__ = MagicMock(return_value=download_resp)
        download_resp.__exit__ = MagicMock(return_value=False)

        mock_requests.post.return_value = login_resp
        # First .get call is info, second is download (via context manager)
        mock_requests.get.side_effect = [info_resp, download_resp]

        with (
            patch.object(provider, "_get_allowed_hosts", return_value={"m.example.com"}),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            result = provider.download(
                "https://m.example.com/view?m=vid1",
                str(tmp_path),
                username="u",
                password="p",
            )
            assert "file_path" in result
            assert "filename" in result
            assert result["info"]["title"] == "Download Test"

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_download_missing_original_url_raises(
        self, mock_requests, provider, tmp_path, _clear_mediacms_env
    ):
        """Raises 502 when original_media_url is missing from info."""
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "auth-tok"}
        login_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.json.return_value = {"title": "No URL"}  # Missing original_media_url
        info_resp.raise_for_status = MagicMock()

        mock_requests.post.return_value = login_resp
        mock_requests.get.return_value = info_resp

        with (
            patch.object(provider, "_get_allowed_hosts", return_value={"m.example.com"}),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            with pytest.raises(HTTPException) as exc:
                provider.download(
                    "https://m.example.com/view?m=vid1",
                    str(tmp_path),
                    username="u",
                    password="p",
                )
            assert exc.value.status_code == 502
            assert "original_media_url" in str(exc.value.detail)

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_download_ssrf_absolute_url_rejected(
        self, mock_requests, provider, tmp_path, _clear_mediacms_env
    ):
        """Absolute original_media_url (potential SSRF) is rejected."""
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "auth-tok"}
        login_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.json.return_value = {
            "title": "SSRF",
            "original_media_url": "http://evil.com/steal",  # Absolute URL = SSRF
        }
        info_resp.raise_for_status = MagicMock()

        mock_requests.post.return_value = login_resp
        mock_requests.get.return_value = info_resp

        with (
            patch.object(provider, "_get_allowed_hosts", return_value={"m.example.com"}),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            with pytest.raises(HTTPException) as exc:
                provider.download(
                    "https://m.example.com/view?m=vid1",
                    str(tmp_path),
                    username="u",
                    password="p",
                )
            assert exc.value.status_code == 502
            assert "invalid media URL" in str(exc.value.detail)

    @patch("app.services.protected_media_plugins.mediacms.requests")
    def test_download_protocol_relative_url_rejected(
        self, mock_requests, provider, tmp_path, _clear_mediacms_env
    ):
        """Protocol-relative original_media_url (//) is rejected as SSRF."""
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "auth-tok"}
        login_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.json.return_value = {
            "title": "SSRF2",
            "original_media_url": "//evil.com/steal",
        }
        info_resp.raise_for_status = MagicMock()

        mock_requests.post.return_value = login_resp
        mock_requests.get.return_value = info_resp

        with (
            patch.object(provider, "_get_allowed_hosts", return_value={"m.example.com"}),
            patch.object(provider, "_get_verify_ssl_for_host", return_value=True),
            patch.object(provider, "_get_stored_credentials", return_value=(None, None)),
        ):
            with pytest.raises(HTTPException) as exc:
                provider.download(
                    "https://m.example.com/view?m=vid1",
                    str(tmp_path),
                    username="u",
                    password="p",
                )
            assert exc.value.status_code == 502


# ===================================================================
# get_public_auth_config
# ===================================================================


class TestPublicAuthConfig:
    """get_public_auth_config exposes host info without credentials."""

    def test_returns_hosts_and_fields(self, provider, _clear_mediacms_env):
        """Config includes host list and auth field descriptors."""
        with (
            patch.object(provider, "_get_allowed_hosts", return_value={"h.example.com"}),
            patch.object(
                provider,
                "_get_db_sources",
                return_value=[{"hostname": "h.example.com", "username": "u", "password": "p"}],
            ),
        ):
            config = provider.get_public_auth_config()
            assert "h.example.com" in config["hosts"]
            assert config["auth_type"] == "user_password"
            assert len(config["fields"]) == 2

    def test_hosts_with_stored_credentials_indicated(self, provider, _clear_mediacms_env):
        """Hosts with pre-configured credentials are listed separately."""
        with (
            patch.object(
                provider,
                "_get_allowed_hosts",
                return_value={"withcreds.example.com", "nocreds.example.com"},
            ),
            patch.object(
                provider,
                "_get_db_sources",
                return_value=[
                    {"hostname": "withcreds.example.com", "username": "u", "password": "p"},
                    {"hostname": "nocreds.example.com", "username": "", "password": ""},
                ],
            ),
        ):
            config = provider.get_public_auth_config()
            assert "withcreds.example.com" in config["hosts_with_stored_credentials"]
            assert "nocreds.example.com" not in config["hosts_with_stored_credentials"]

    def test_empty_when_no_hosts(self, provider, _clear_mediacms_env):
        """Returns empty dict when no hosts are configured."""
        with patch.object(provider, "_get_allowed_hosts", return_value=set()):
            assert provider.get_public_auth_config() == {}
