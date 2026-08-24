"""MediacmsProvider plugin for protected media downloads.

Handles password-protected MediaCMS installations configured via database
settings (Admin UI) or environment variables (legacy fallback).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urljoin
from urllib.parse import urlparse
from urllib.parse import urlunparse

import requests
from fastapi import HTTPException

from app.services.protected_media_providers import ProtectedMediaProvider
from app.utils.url_validation import assert_safe_outbound_url

logger = logging.getLogger(__name__)


class MediacmsProvider(ProtectedMediaProvider):
    """ProtectedMediaProvider for MediaCMS-based sites.

    Hostnames are configured via the Admin UI (stored in database) or via the
    legacy MEDIACMS_ALLOWED_HOSTS environment variable (comma-separated list).

    SSL verification can be overridden per-host in the Admin UI or globally
    via MEDIACMS_VERIFY_SSL=false (env).
    """

    def _get_db_sources(self) -> list[dict]:
        """Load MediaCMS sources from legacy system settings."""
        try:
            from app.db.base import SessionLocal
            from app.services.system_settings_service import get_media_sources

            db = SessionLocal()
            try:
                sources = get_media_sources(db)
                return [s for s in sources if s.get("provider_type") == "mediacms"]
            finally:
                db.close()
        except Exception:
            return []

    def _get_user_media_sources(self, user_id: int | None = None) -> list[dict]:
        """Load MediaCMS sources from per-user media source table.

        Returns all sources visible to the user: own + shared by others.
        If user_id is None, returns all shared sources.
        """
        try:
            from sqlalchemy import or_

            from app.db.base import SessionLocal
            from app.models.user_media_source import UserMediaSource
            from app.utils.encryption import decrypt_api_key

            db = SessionLocal()
            try:
                if user_id is not None:
                    # Order: own sources first, then shared — so own credentials take priority
                    sources = (
                        db.query(UserMediaSource)
                        .filter(
                            UserMediaSource.provider_type == "mediacms",
                            UserMediaSource.is_active == True,  # noqa: E712
                            or_(
                                UserMediaSource.user_id == user_id,
                                UserMediaSource.is_shared == True,  # noqa: E712
                            ),
                        )
                        .order_by(
                            (UserMediaSource.user_id == user_id).desc(),
                        )
                        .all()
                    )
                else:
                    # No user context — return all shared sources
                    sources = (
                        db.query(UserMediaSource)
                        .filter(
                            UserMediaSource.provider_type == "mediacms",
                            UserMediaSource.is_active == True,  # noqa: E712
                            UserMediaSource.is_shared == True,  # noqa: E712
                        )
                        .all()
                    )

                result = []
                for s in sources:
                    password = None
                    if s.password:
                        password = decrypt_api_key(str(s.password))
                    result.append(
                        {
                            "hostname": s.hostname,
                            "provider_type": s.provider_type,
                            "username": s.username or "",
                            "password": password or "",
                            "verify_ssl": s.verify_ssl,
                            "label": s.label or "",
                            "user_id": s.user_id,
                        }
                    )
                return result
            finally:
                db.close()
        except Exception as e:
            logger.debug("Failed to load per-user media sources: %s", e)
            return []

    def _get_all_sources(self, user_id: int | None = None) -> list[dict]:
        """Get combined media sources: per-user + legacy system + env."""
        # Per-user sources take priority (checked first)
        user_sources = self._get_user_media_sources(user_id)
        # Legacy system-level sources
        system_sources = self._get_db_sources()
        return user_sources + system_sources

    @property
    def allowed_hosts(self) -> set[str]:
        return self._get_allowed_hosts()

    def _get_allowed_hosts(self, user_id: int | None = None) -> set[str]:
        hosts: set[str] = set()
        # Per-user + system DB sources
        for s in self._get_all_sources(user_id):
            h = s.get("hostname", "").strip()
            if h:
                hosts.add(h)
        # Env fallback (legacy)
        raw = os.getenv("MEDIACMS_ALLOWED_HOSTS", "")
        for h in raw.split(","):
            h = h.strip()
            if h:
                hosts.add(h)
        return hosts

    def _get_verify_ssl_for_host(self, hostname: str, user_id: int | None = None) -> bool:
        """Get SSL verification setting for a specific host."""
        for s in self._get_all_sources(user_id):
            if s.get("hostname") == hostname:
                return bool(s.get("verify_ssl", True))
        # Env fallback
        return os.getenv("MEDIACMS_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

    def _get_stored_credentials(
        self, hostname: str, user_id: int | None = None
    ) -> tuple[str | None, str | None]:
        """Get stored credentials for a specific host.

        Checks per-user sources first (own, then shared), then system-level.
        """
        for s in self._get_all_sources(user_id):
            if s.get("hostname") == hostname:
                username = s.get("username") or None
                password = s.get("password") or None
                if username and password:
                    return username, password
        return None, None

    @property
    def verify_ssl(self) -> bool:
        """Global SSL verification fallback (used when host is unknown)."""
        return os.getenv("MEDIACMS_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

    def can_handle(self, url: str, user_id: int | None = None) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                return False
            if parsed.netloc not in self._get_allowed_hosts(user_id):
                return False

            # Either ?m=<token> query param or /api/v1/media/<token> path
            query = parse_qs(parsed.query)
            if "m" in query and query["m"]:
                return True

            path_parts = [p for p in parsed.path.split("/") if p]
            return (
                len(path_parts) >= 3
                and path_parts[0] == "api"
                and path_parts[1] == "v1"
                and path_parts[2] == "media"
            )
        except Exception:
            return False

    # --- internal helpers -------------------------------------------------

    def _get_token_and_base_url(self, url: str, user_id: int | None = None) -> tuple[str, str]:
        parsed = urlparse(url)

        if parsed.netloc not in self._get_allowed_hosts(user_id):
            raise HTTPException(
                status_code=400,
                detail="URL hostname is not in the configured media sources",
            )

        query = parse_qs(parsed.query)
        friendly_token: str | None = None

        # Primary: view URL with ?m=<token>
        if "m" in query and query["m"]:
            friendly_token = query["m"][0]
        else:
            # Fallback: /api/v1/media/<token>
            path_parts = [p for p in parsed.path.split("/") if p]
            if (
                len(path_parts) >= 3
                and path_parts[0] == "api"
                and path_parts[1] == "v1"
                and path_parts[2] == "media"
            ):
                friendly_token = path_parts[3] if len(path_parts) >= 4 else None

        if not friendly_token:
            raise HTTPException(
                status_code=400,
                detail="Missing media token (m query param or /api/v1/media/<token>)",
            )

        base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        return friendly_token, base_url

    def _login_and_get_info(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> tuple[str, str, dict[str, Any], str]:
        """Authenticate against MediaCMS and fetch media JSON.

        Returns:
            Tuple of (friendly_token, base_url, info_dict, auth_token).
        """
        media_user = username
        media_pass = password

        # Fall back to stored credentials if none provided in request
        if not media_user or not media_pass:
            parsed = urlparse(url)
            stored_user, stored_pass = self._get_stored_credentials(parsed.netloc, user_id=user_id)
            if not media_user:
                media_user = stored_user
            if not media_pass:
                media_pass = stored_pass

        if not media_user or not media_pass:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Credentials for this media source are not configured. "
                    "Either provide credentials in the request or configure "
                    "them in Settings > Media Sources."
                ),
            )

        friendly_token, base_url = self._get_token_and_base_url(url, user_id=user_id)
        parsed = urlparse(url)
        host_verify_ssl = self._get_verify_ssl_for_host(parsed.netloc, user_id=user_id)
        auth_payload = {"username": media_user, "password": media_pass}

        login_url = f"{base_url}/api/v1/login"
        info_url = f"{base_url}/api/v1/media/{friendly_token}"

        try:
            # Defence in depth over the schema guard on the stored hostname
            # (`schemas/media_source.py`): rows configured before that guard existed are
            # still in the database, and a hostname that resolved publicly at write time
            # can resolve to 169.254.169.254 by the time it is fetched.
            assert_safe_outbound_url(login_url, purpose="MediaCMS login")
            login_resp = requests.post(
                url=login_url,
                data=auth_payload,
                timeout=30,
                verify=host_verify_ssl,
            )
            login_resp.raise_for_status()
            token_data = login_resp.json()
            auth_token = token_data.get("token")
            if not auth_token:
                raise HTTPException(
                    status_code=502,
                    detail="MediaCMS login did not return an auth token",
                )

            headers = {
                "authorization": f"Token {auth_token}",
                "accept": "application/json",
            }
            assert_safe_outbound_url(info_url, purpose="MediaCMS media info")
            info_resp = requests.get(
                url=info_url,
                headers=headers,
                timeout=30,
                verify=host_verify_ssl,
            )
            info_resp.raise_for_status()
            info = info_resp.json()

        except HTTPException:
            raise
        except requests.exceptions.RequestException as e:
            # Re-wrap as HTTPException for consistency with FastAPI error handling
            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch media information from MediaCMS: {e}",
            ) from e

        return friendly_token, base_url, info, auth_token

    # --- ProtectedMediaProvider implementation ---------------------------

    def get_public_auth_config(self) -> dict[str, Any]:
        """Expose public auth configuration for this provider.

        Returns host list with auth requirements. Hosts with stored
        credentials indicate that credentials are optional (pre-configured).
        """
        hosts = sorted(self.allowed_hosts)
        if not hosts:
            return {}

        # Check which hosts have stored credentials
        db_sources = self._get_db_sources()
        hosts_with_creds = {
            s["hostname"] for s in db_sources if s.get("username") and s.get("password")
        }

        return {
            "hosts": hosts,
            "hosts_with_stored_credentials": sorted(hosts_with_creds),
            "auth_type": "user_password",
            "fields": [
                {
                    "name": "media_username",
                    "label": "Media username",
                    "type": "text",
                },
                {
                    "name": "media_password",
                    "label": "Media password",
                    "type": "password",
                },
            ],
        }

    def _build_media_info(
        self,
        friendly_token: str,
        base_url: str,
        info: dict[str, Any],
        url: str,
    ) -> dict[str, Any]:
        """Build a yt-dlp-compatible media info dict from MediaCMS API response."""
        title = info.get("title") or info.get("name") or friendly_token

        # MediaCMS may return relative thumbnail paths like
        # "/media/original/thumbnails/..."; normalize them to absolute URLs.
        raw_thumbnail = info.get("thumbnail_url")
        thumbnail_url: str | None = None
        if raw_thumbnail:
            parsed_thumb = urlparse(str(raw_thumbnail))
            if parsed_thumb.scheme:
                thumbnail_url = str(raw_thumbnail)
            else:
                thumbnail_url = urljoin(base_url, str(raw_thumbnail))

        media_info: dict[str, Any] = {
            "id": friendly_token,
            "title": title,
            "description": info.get("description"),
            "uploader": info.get("owner") or info.get("user"),
            "duration": info.get("duration"),
            "extractor": "mediacms",
            "thumbnail": thumbnail_url,
            "original_media_url": info.get("original_media_url"),
            "source": "mediacms",
            "original_url": url,
        }
        media_info["mediacms_raw"] = info
        media_info["mediacms_base_url"] = base_url
        return media_info

    def extract_info(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        friendly_token, base_url, info, _token = self._login_and_get_info(
            url, username=username, password=password, user_id=user_id
        )
        return self._build_media_info(friendly_token, base_url, info, url)

    def download(
        self,
        url: str,
        output_path: str,
        progress_callback: Callable[[int, str], None] | None = None,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        friendly_token, base_url, info, auth_token = self._login_and_get_info(
            url, username=username, password=password, user_id=user_id
        )

        original_media_url = info.get("original_media_url")
        if not original_media_url:
            raise HTTPException(
                status_code=502,
                detail="MediaCMS media info is missing 'original_media_url'",
            )

        # Validate original_media_url to prevent SSRF via malicious server response
        if "://" in str(original_media_url) or str(original_media_url).startswith("//"):
            raise HTTPException(
                status_code=502,
                detail="MediaCMS returned an invalid media URL",
            )

        download_url = f"{base_url}{original_media_url}"
        parsed = urlparse(url)
        host_verify_ssl = self._get_verify_ssl_for_host(parsed.netloc, user_id=user_id)

        # Use the auth token for the download request (protected files need it)
        download_headers = {"authorization": f"Token {auth_token}"}
        file_path = ""

        try:
            if progress_callback:
                progress_callback(20, "Downloading media from authenticated source...")

            # Re-checked rather than trusted from the login leg: `download_url` is built
            # from a path the MediaCMS server chose, and the host is resolved again here.
            assert_safe_outbound_url(download_url, purpose="MediaCMS media download")
            with requests.get(
                download_url,
                stream=True,
                timeout=300,
                verify=host_verify_ssl,
                headers=download_headers,
            ) as resp:
                resp.raise_for_status()
                total_bytes = int(resp.headers.get("Content-Length", "0")) or None
                downloaded = 0

                raw_title = info.get("title") or info.get("name") or friendly_token
                clean_title = re.sub(r"[^\w\-_\. ]", "_", str(raw_title))[:200]
                filename = clean_title if "." in clean_title else f"{clean_title}.mp4"

                file_path = os.path.join(output_path, filename)
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_bytes and total_bytes > 0:
                            progress = int(20 + (downloaded / total_bytes) * 40)
                            progress_callback(min(progress, 60), "Downloading media...")

        except HTTPException:
            raise
        except requests.exceptions.RequestException as e:
            # Clean up partial file on failure
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            raise HTTPException(
                status_code=502,
                detail=f"Failed to download media file from MediaCMS: {e}",
            ) from e

        # Build info dict from already-fetched data (no redundant second login)
        media_info = self._build_media_info(friendly_token, base_url, info, url)

        return {
            "file_path": file_path,
            "filename": filename,
            "info": media_info,
        }


# Default export for plugin loader
provider = MediacmsProvider()
