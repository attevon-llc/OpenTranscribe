"""Protected-media provider for MediaCMS sites that require a login.

A MediaCMS media page is addressed either as ``<site>/view?m=<token>`` or through the REST
path ``<site>/api/v1/media/<token>``. For such a URL on a configured media source this
provider signs in with the user's site credentials (given with the request or stored with
the source), reads the media's metadata from the REST API and streams the original file.

Which sources apply, their TLS setting and their stored credentials come from
``MediaSourceResolver``. Every outbound request is pinned to a validated public address
and never follows a redirect, and the file is only fetched from the configured origin.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import ParseResult
from urllib.parse import parse_qs
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests

from app.core.exceptions import InvalidInputError
from app.core.exceptions import UpstreamServiceError
from app.services.media_source_resolution import MediaSourceResolver
from app.services.protected_media_providers import ProtectedMediaProvider
from app.utils.media_origin import _pin_media_url
from app.utils.media_origin import resolve_same_origin_url
from app.utils.url_validation import pinned_requests_session

logger = logging.getLogger(__name__)

_API_MEDIA_SEGMENTS = ["api", "v1", "media"]
_REQUEST_TIMEOUT_SECONDS = 30
_DOWNLOAD_TIMEOUT_SECONDS = 300
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_FILENAME_CHARS = 200
_UNSAFE_FILENAME_CHARS = re.compile(r"[^\w\-. ]")

_NOT_CONFIGURED = "URL hostname is not in the configured media sources"
_MEDIA_NOT_NAMED = "Missing media token (m query param or /api/v1/media/<token>)"
_NO_CREDENTIALS = (
    "Credentials for this media source are not configured. Either provide credentials in "
    "the request or configure them in Settings > Media Sources."
)
_LOGIN_GAVE_NO_KEY = "MediaCMS login did not return an auth token"
_INFO_FAILED = "Could not fetch media information from the configured media source."
_NO_MEDIA_URL = "MediaCMS media info is missing 'original_media_url'"
_BAD_MEDIA_URL = "MediaCMS returned an invalid media URL"
_DOWNLOAD_FAILED = "Could not download the media file from the configured media source."

_PUBLIC_FIELDS = [
    {"name": "media_username", "label": "Media username", "type": "text"},
    {"name": "media_password", "label": "Media password", "type": "password"},
]


def _path_segments(parsed: ParseResult) -> list[str]:
    return [segment for segment in parsed.path.split("/") if segment]


def _token_in_query(parsed: ParseResult) -> str | None:
    """The first non-empty ``m`` query value, if any."""
    values = parse_qs(parsed.query).get("m")
    return values[0] if values else None


def _is_media_api_path(segments: list[str]) -> bool:
    return segments[: len(_API_MEDIA_SEGMENTS)] == _API_MEDIA_SEGMENTS


def _download_filename(info: dict[str, Any], friendly_token: str) -> str:
    """A filesystem-safe name for the downloaded file, built from the media's title.

    ``.mp4`` is appended only when the cleaned name contains no dot at all.
    """
    label = info.get("title") or info.get("name") or friendly_token
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", str(label))[:_MAX_FILENAME_CHARS]
    return cleaned if "." in cleaned else f"{cleaned}.mp4"


def _thumbnail_url(raw: Any, base_url: str) -> str | None:
    """Absolute thumbnail URL: kept when it already has a scheme, else resolved on the site."""
    if not raw:
        return None
    if urlparse(raw).scheme:
        return str(raw)
    return urljoin(base_url, str(raw))


class MediacmsProvider(MediaSourceResolver, ProtectedMediaProvider):
    """Signs in to a configured MediaCMS site and fetches media on the user's behalf."""

    def can_handle(self, url: str, user_id: int | None = None) -> bool:
        """Claim a MediaCMS media URL on one of the user's configured sources.

        Args:
            url: The submitted URL.
            user_id: The requesting user, whose own and shared sources count.

        Returns:
            True for an http(s) URL on a configured host that carries an ``m`` query
            value or an ``/api/v1/media`` path. Any failure while deciding is False.
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False
            if not self._is_allowed_host(parsed, user_id):
                return False
            return bool(_token_in_query(parsed)) or _is_media_api_path(_path_segments(parsed))
        except Exception as exc:
            logger.debug("MediaCMS provider could not evaluate %s: %s", url, exc)
            return False

    def _get_token_and_base_url(self, url: str, user_id: int | None = None) -> tuple[str, str]:
        """Split a MediaCMS URL into its media token and the site's origin.

        Args:
            url: The media URL.
            user_id: The requesting user, whose own and shared sources count.

        Returns:
            ``(friendly_token, "scheme://host[:port]")``.

        Raises:
            InvalidInputError: The host is not a configured source, or the URL names no media.
        """
        parsed = urlparse(url)
        if not self._is_allowed_host(parsed, user_id):
            raise InvalidInputError(_NOT_CONFIGURED)

        token = _token_in_query(parsed)
        if not token:
            segments = _path_segments(parsed)
            if _is_media_api_path(segments) and len(segments) > len(_API_MEDIA_SEGMENTS):
                token = segments[len(_API_MEDIA_SEGMENTS)]
        if not token:
            raise InvalidInputError(_MEDIA_NOT_NAMED)
        return token, f"{parsed.scheme}://{parsed.netloc}"

    def _login_and_get_info(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> tuple[str, str, dict[str, Any], str]:
        """Sign in to the site and read the media's metadata from its REST API.

        A username or password missing from the request is taken from the stored source
        for this host, each field on its own.

        Args:
            url: The media URL.
            username: Site username from the request, if any.
            password: Site password from the request, if any.
            user_id: The requesting user, whose own and shared sources count.

        Returns:
            ``(friendly_token, base_url, media_info, auth_token)``.

        Raises:
            InvalidInputError: No usable credentials, an unconfigured host, no media token,
                or a request address that may not be fetched.
            UpstreamServiceError: The site refused the login or the metadata request.
        """
        parsed = urlparse(url)
        if not (username and password):
            stored_username, stored_password = self._get_stored_credentials(
                parsed.netloc, user_id=user_id, scheme=parsed.scheme
            )
            username = username or stored_username
            password = password or stored_password
        if not (username and password):
            raise InvalidInputError(_NO_CREDENTIALS)

        friendly_token, base_url = self._get_token_and_base_url(url, user_id=user_id)
        verify = self._get_verify_ssl_for_host(parsed.netloc, user_id=user_id, scheme=parsed.scheme)

        try:
            # Defence in depth over the schema guard on the stored hostname
            # (`schemas/media_source.py`): rows configured before that guard existed are
            # still in the database, and a hostname that resolved publicly at write time
            # can resolve to 169.254.169.254 by the time it is fetched. `_pin_media_url`
            # pins the checked address and `allow_redirects=False` refuses to follow a
            # redirect to anywhere else — see its docstring for both gaps this closes.
            login_target = _pin_media_url(f"{base_url}/api/v1/login", purpose="MediaCMS login")
            with pinned_requests_session(login_target) as session:
                login_response = session.post(
                    login_target.url,
                    data={"username": username, "password": password},
                    headers={**login_target.headers},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                    verify=verify,
                    allow_redirects=False,
                )
                login_response.raise_for_status()
                auth_token = login_response.json().get("token")
            if not auth_token:
                raise UpstreamServiceError(_LOGIN_GAVE_NO_KEY)

            info_target = _pin_media_url(
                f"{base_url}/api/v1/media/{friendly_token}", purpose="MediaCMS media info"
            )
            with pinned_requests_session(info_target) as session:
                info_response = session.get(
                    info_target.url,
                    headers={
                        "authorization": f"Token {auth_token}",
                        "accept": "application/json",
                        **info_target.headers,
                    },
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                    verify=verify,
                    allow_redirects=False,
                )
                info_response.raise_for_status()
                media_info: dict[str, Any] = info_response.json()
        except requests.exceptions.RequestException as exc:
            # The raw requests exception carries the FULL internal URL, including
            # host:port — never returned to the caller (#859).
            logger.exception("MediaCMS sign-in or media-info request failed")
            raise UpstreamServiceError(_INFO_FAILED) from exc

        return friendly_token, base_url, media_info, str(auth_token)

    def _build_media_info(
        self, friendly_token: str, base_url: str, info: dict[str, Any], url: str
    ) -> dict[str, Any]:
        """Map MediaCMS's media record onto the yt-dlp-style metadata the importer reads.

        Args:
            friendly_token: The media's token.
            base_url: The site's origin.
            info: The media record returned by the REST API.
            url: The URL the user submitted.

        Returns:
            The metadata dict, with the raw record kept under ``mediacms_raw``.
        """
        return {
            "id": friendly_token,
            "title": info.get("title") or info.get("name") or friendly_token,
            "description": info.get("description"),
            "uploader": info.get("owner") or info.get("user"),
            "duration": info.get("duration"),
            "extractor": "mediacms",
            "thumbnail": _thumbnail_url(info.get("thumbnail_url"), base_url),
            "original_media_url": info.get("original_media_url"),
            "source": "mediacms",
            "original_url": url,
            "mediacms_raw": info,
            "mediacms_base_url": base_url,
        }

    def extract_info(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Sign in and return the media's metadata without downloading it.

        Args:
            url: The media URL.
            username: Site username from the request, if any.
            password: Site password from the request, if any.
            user_id: The requesting user.

        Returns:
            The metadata dict from ``_build_media_info``.
        """
        friendly_token, base_url, media_info, _ = self._login_and_get_info(
            url, username=username, password=password, user_id=user_id
        )
        return self._build_media_info(friendly_token, base_url, media_info, url)

    def download(
        self,
        url: str,
        output_path: str,
        progress_callback: Callable[[int, str], None] | None = None,
        username: str | None = None,
        password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Sign in once and stream the media's original file into ``output_path``.

        Progress is reported at 20 when the transfer starts and then, when the size is
        known, rises towards 60 as data arrives.

        Args:
            url: The media URL.
            output_path: Directory the file is written into.
            progress_callback: Optional ``(percent, message)`` reporter.
            username: Site username from the request, if any.
            password: Site password from the request, if any.
            user_id: The requesting user.

        Returns:
            ``{"file_path", "filename", "info"}``.

        Raises:
            InvalidInputError: As for ``_login_and_get_info``, or the file's address may
                not be fetched.
            UpstreamServiceError: The site gave no usable file URL, or the transfer failed.
        """
        friendly_token, base_url, media_info, auth_token = self._login_and_get_info(
            url, username=username, password=password, user_id=user_id
        )

        server_media_url = media_info.get("original_media_url")
        if not server_media_url:
            raise UpstreamServiceError(_NO_MEDIA_URL)
        download_url = resolve_same_origin_url(base_url, str(server_media_url))
        if download_url is None:
            raise UpstreamServiceError(_BAD_MEDIA_URL)

        parsed = urlparse(url)
        verify = self._get_verify_ssl_for_host(parsed.netloc, user_id=user_id, scheme=parsed.scheme)

        if progress_callback:
            progress_callback(20, "Starting authenticated media download...")

        filename = _download_filename(media_info, friendly_token)
        file_path = os.path.join(output_path, filename)

        try:
            # Re-checked rather than trusted from the login leg: `download_url` is built
            # from a path the MediaCMS server chose, and the host is resolved again here.
            # Pinned + `allow_redirects=False` for the same reason as the login/info legs
            # above: a redirect on the download leg writes the redirected response BODY to
            # a file the user then owns, which is worse than the other two legs leaking it
            # back in a JSON field.
            download_target = _pin_media_url(download_url, purpose="MediaCMS download")
            with (
                pinned_requests_session(download_target) as session,
                session.get(
                    download_target.url,
                    stream=True,
                    timeout=_DOWNLOAD_TIMEOUT_SECONDS,
                    verify=verify,
                    headers={"authorization": f"Token {auth_token}", **download_target.headers},
                    allow_redirects=False,
                ) as response,
            ):
                response.raise_for_status()
                total_bytes = int(response.headers.get("Content-Length") or 0)
                self._write_stream(response, file_path, total_bytes, progress_callback)
        except requests.exceptions.RequestException as exc:
            # Clean up partial file on failure. The raw requests exception
            # carries the FULL internal URL, including host:port — never
            # returned to the caller (#859).
            if os.path.exists(file_path):
                os.remove(file_path)
            logger.exception("MediaCMS media download failed")
            raise UpstreamServiceError(_DOWNLOAD_FAILED) from exc

        return {
            "file_path": file_path,
            "filename": filename,
            "info": self._build_media_info(friendly_token, base_url, media_info, url),
        }

    @staticmethod
    def _write_stream(
        response: Any,
        file_path: str,
        total_bytes: int,
        progress_callback: Callable[[int, str], None] | None,
    ) -> None:
        """Write the response body to ``file_path``, reporting 20-60 progress when sized."""
        received = 0
        with open(file_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                handle.write(chunk)
                received += len(chunk)
                if total_bytes and progress_callback:
                    percent = min(int(20 + 40 * received / total_bytes), 60)
                    progress_callback(percent, "Downloading media...")

    def get_public_auth_config(self, user_id: int | None = None) -> dict[str, Any]:
        """Describe the login form for the user's MediaCMS hosts, with no secret in it.

        Args:
            user_id: The user the description is for.

        Returns:
            The hosts, the hosts that already have stored credentials, the auth type and
            the two form fields; an empty dict when the user has no MediaCMS host.
        """
        hosts = sorted(self._get_allowed_hosts(user_id))
        if not hosts:
            return {}
        return {
            "hosts": hosts,
            "hosts_with_stored_credentials": sorted(self._hosts_with_stored_credentials(user_id)),
            "auth_type": "user_password",
            "fields": [dict(field) for field in _PUBLIC_FIELDS],
        }


provider = MediacmsProvider()
