"""Which configured media sources apply to a user and a host.

Sources come from the caller's own and same-tenant shared rows (``media_source_access``), then
the operator's system list, then the legacy ``MEDIACMS_*`` environment variables. Every host
decision uses the one identity rule in ``utils/media_origin.host_matches``: scheme, host and
port, with a scheme-less source meaning ``https``. The MediaCMS protected-media plugin inherits
these lookups.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.services.media_source_access import query_user_media_sources
from app.utils.media_origin import host_matches
from app.utils.media_origin import source_base_url

logger = logging.getLogger(__name__)


class MediaSourceResolver:
    """Source lookups for a protected-media provider.

    Hostnames are configured via the Admin UI (stored in database) or via the
    legacy MEDIACMS_ALLOWED_HOSTS environment variable (comma-separated list).

    SSL verification can be overridden per-host in the Admin UI or globally
    via MEDIACMS_VERIFY_SSL=false (env).
    """

    def _get_db_sources(self) -> list[dict]:
        """Load MediaCMS sources from legacy system settings."""
        try:
            from app.db.base import SessionLocal
            from app.services.system_settings_service import get_system_media_sources

            db = SessionLocal()
            try:
                sources = get_system_media_sources(db)
                return [s for s in sources if s.get("provider_type") == "mediacms"]
            finally:
                db.close()
        except SQLAlchemyError:
            logger.warning("Failed to load system media sources", exc_info=True)
            return []

    def _get_user_media_sources(self, user_id: int | None = None) -> list[dict]:
        """Load MediaCMS sources from the per-user media source table.

        See ``media_source_access.query_user_media_sources`` for which rows are visible.
        """
        try:
            from app.db.base import SessionLocal
            from app.utils.encryption import SecretDecryptionError
            from app.utils.encryption import decrypt_api_key

            db = SessionLocal()
            try:
                sources = query_user_media_sources(db, user_id)

                result = []
                for s in sources:
                    password = None
                    if s.password:
                        try:
                            password = decrypt_api_key(str(s.password))
                        except SecretDecryptionError:
                            # Skip rather than offer the host with an empty password.
                            logger.warning(
                                "Skipping media source %s: stored password cannot be decrypted",
                                s.hostname,
                            )
                            continue
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
        except SQLAlchemyError as e:
            logger.warning("Failed to load per-user media sources: %s", e, exc_info=True)
            return []

    def _get_all_sources(self, user_id: int | None = None) -> list[dict]:
        """Get combined media sources: per-user + legacy system + env."""
        # Per-user sources take priority (checked first)
        user_sources = self._get_user_media_sources(user_id)
        # Legacy system-level sources
        system_sources = self._get_db_sources()
        return user_sources + system_sources

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

    def _get_verify_ssl_for_host(
        self, hostname: str, user_id: int | None = None, *, scheme: str
    ) -> bool:
        """SSL verification setting for the source at ``scheme`` + ``host[:port]``."""
        for s in self._get_all_sources(user_id):
            if host_matches(s.get("hostname", ""), hostname, scheme):
                return bool(s.get("verify_ssl", True))
        # Env fallback
        return os.getenv("MEDIACMS_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

    def _get_stored_credentials(
        self, hostname: str, user_id: int | None = None, *, scheme: str
    ) -> tuple[str | None, str | None]:
        """Stored credentials for the source at ``scheme`` + ``host[:port]``.

        Checks per-user sources first (own, then shared), then system-level.
        """
        for s in self._get_all_sources(user_id):
            if host_matches(s.get("hostname", ""), hostname, scheme):
                username = s.get("username") or None
                password = s.get("password") or None
                if username and password:
                    return username, password
        return None, None

    @property
    def verify_ssl(self) -> bool:
        """Global SSL verification fallback (used when host is unknown)."""
        return os.getenv("MEDIACMS_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

    def _configured_base_url(self, parsed: Any, user_id: int | None) -> str | None:
        """The configured origin a parsed URL is on, built from the configuration, or None.

        Requests that carry a credential are addressed to this value, never to the
        submitted URL's own scheme and authority.
        """
        for configured in sorted(self._get_allowed_hosts(user_id)):
            if host_matches(configured, parsed.netloc, parsed.scheme):
                return source_base_url(configured)
        return None

    def _is_allowed_host(self, parsed: Any, user_id: int | None) -> bool:
        return self._configured_base_url(parsed, user_id) is not None

    def _hosts_with_stored_credentials(self, user_id: int | None = None) -> set[str]:
        """Configured hostnames, as stored, whose source carries a username and a password."""
        return {
            s["hostname"]
            for s in self._get_all_sources(user_id)
            if s.get("hostname") and s.get("username") and s.get("password")
        }
