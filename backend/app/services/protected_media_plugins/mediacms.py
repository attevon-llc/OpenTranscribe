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

from app.core.exceptions import DomainError
from app.core.exceptions import InvalidInputError
from app.core.exceptions import UpstreamServiceError
from app.services.media_source_resolution import MediaSourceResolver
from app.services.protected_media_providers import ProtectedMediaProvider
from app.utils.media_origin import _pin_media_url
from app.utils.media_origin import resolve_same_origin_url
from app.utils.url_validation import pinned_requests_session

logger = logging.getLogger(__name__)


class MediacmsProvider(MediaSourceResolver, ProtectedMediaProvider):
    """ProtectedMediaProvider for MediaCMS-based sites; source lookups come from the mixin."""

    @property
    def allowed_hosts(self) -> set[str]:
        return self._get_allowed_hosts()

    def can_handle(self, url: str, user_id: int | None = None) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                return False
            if not self._is_allowed_host(parsed, user_id):
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

        if not self._is_allowed_host(parsed, user_id):
            raise InvalidInputError(
                "URL hostname is not in the configured media sources",
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
            raise InvalidInputError(
                "Missing media token (m query param or /api/v1/media/<token>)",
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
            stored_user, stored_pass = self._get_stored_credentials(
                parsed.netloc, user_id=user_id, scheme=parsed.scheme
            )
            if not media_user:
                media_user = stored_user
            if not media_pass:
                media_pass = stored_pass

        if not media_user or not media_pass:
            raise InvalidInputError(
                "Credentials for this media source are not configured. "
                "Either provide credentials in the request or configure "
                "them in Settings > Media Sources."
            )

        friendly_token, base_url = self._get_token_and_base_url(url, user_id=user_id)
        parsed = urlparse(url)
        host_verify_ssl = self._get_verify_ssl_for_host(
            parsed.netloc, user_id=user_id, scheme=parsed.scheme
        )
        auth_payload = {"username": media_user, "password": media_pass}

        login_url = f"{base_url}/api/v1/login"
        info_url = f"{base_url}/api/v1/media/{friendly_token}"

        try:
            # Defence in depth over the schema guard on the stored hostname
            # (`schemas/media_source.py`): rows configured before that guard existed are
            # still in the database, and a hostname that resolved publicly at write time
            # can resolve to 169.254.169.254 by the time it is fetched. `_pin_media_url`
            # pins the checked address and `allow_redirects=False` refuses to follow a
            # redirect to anywhere else — see its docstring for both gaps this closes.
            login_target = _pin_media_url(login_url, purpose="MediaCMS login")
            with pinned_requests_session(login_target) as login_session:
                login_resp = login_session.post(
                    login_target.url,
                    data=auth_payload,
                    headers=login_target.headers,
                    timeout=30,
                    verify=host_verify_ssl,
                    allow_redirects=False,
                )
            login_resp.raise_for_status()
            token_data = login_resp.json()
            auth_token = token_data.get("token")
            if not auth_token:
                raise UpstreamServiceError(
                    "MediaCMS login did not return an auth token",
                )

            headers = {
                "authorization": f"Token {auth_token}",
                "accept": "application/json",
            }
            info_target = _pin_media_url(info_url, purpose="MediaCMS media info")
            with pinned_requests_session(info_target) as info_session:
                info_resp = info_session.get(
                    info_target.url,
                    headers={**headers, **info_target.headers},
                    timeout=30,
                    verify=host_verify_ssl,
                    allow_redirects=False,
                )
            info_resp.raise_for_status()
            info = info_resp.json()

        except DomainError:
            raise
        except requests.exceptions.RequestException as e:
            # Re-wrap as a DomainError so the API layer renders it.
            # The raw requests exception carries the FULL internal URL, including
            # host:port — never returned to the caller (#859).
            logger.exception("Failed to fetch media information from MediaCMS")
            raise UpstreamServiceError(
                "Could not fetch media information from the configured media source.",
            ) from e

        return friendly_token, base_url, info, auth_token

    # --- ProtectedMediaProvider implementation ---------------------------

    def get_public_auth_config(self, user_id: int | None = None) -> dict[str, Any]:
        """Expose public auth configuration for this provider.

        Returns host list with auth requirements. Hosts with stored
        credentials indicate that credentials are optional (pre-configured).
        Per-user sources appear only for ``user_id`` (own + shared in its tenant).
        """
        hosts = sorted(self._get_allowed_hosts(user_id))
        if not hosts:
            return {}

        hosts_with_creds = self._hosts_with_stored_credentials(user_id)

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
            raise UpstreamServiceError(
                "MediaCMS media info is missing 'original_media_url'",
            )

        download_url = resolve_same_origin_url(base_url, str(original_media_url))
        if download_url is None:
            raise UpstreamServiceError(
                "MediaCMS returned an invalid media URL",
            )
        parsed = urlparse(url)
        host_verify_ssl = self._get_verify_ssl_for_host(
            parsed.netloc, user_id=user_id, scheme=parsed.scheme
        )

        # Use the auth token for the download request (protected files need it)
        download_headers = {"authorization": f"Token {auth_token}"}
        file_path = ""

        try:
            if progress_callback:
                progress_callback(20, "Downloading media from authenticated source...")

            # Re-checked rather than trusted from the login leg: `download_url` is built
            # from a path the MediaCMS server chose, and the host is resolved again here.
            # Pinned + `allow_redirects=False` for the same reason as the login/info legs
            # above: a redirect on the download leg writes the redirected response BODY to
            # a file the user then owns, which is worse than the other two legs leaking it
            # back in a JSON field.
            download_target = _pin_media_url(download_url, purpose="MediaCMS media download")
            with (
                pinned_requests_session(download_target) as download_session,
                download_session.get(
                    download_target.url,
                    stream=True,
                    timeout=300,
                    verify=host_verify_ssl,
                    headers={**download_headers, **download_target.headers},
                    allow_redirects=False,
                ) as resp,
            ):
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

        except DomainError:
            raise
        except requests.exceptions.RequestException as e:
            # Clean up partial file on failure. The raw requests exception
            # carries the FULL internal URL, including host:port — never
            # returned to the caller (#859).
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            logger.exception("Failed to download media file from MediaCMS")
            raise UpstreamServiceError(
                "Could not download the media file from the configured media source.",
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
