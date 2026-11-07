"""Read side of the super-admin allowlist of private-network media hosts.

Media sources are fetched server-side with the user's credentials, so a private, loopback or
unresolvable address is refused by default (``utils/url_validation``). An internal MediaCMS
server is a legitimate deployment, so a super administrator can list the exact origins
(scheme, host and port) that are allowed to be private (``services/media_private_hosts``
writes the list). The cloud-metadata carve-out in ``url_validation`` is never lifted by it.

Lives in ``utils`` because both the request schemas and the pinned fetch consult it.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.core import settings_cache
from app.utils.media_origin import same_source_origin

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SETTING_KEY = "media_sources.private_hosts"


def get_private_hosts(db: Session) -> list[str]:
    """The stored allowlist (normalised ``scheme://host[:port]`` strings)."""
    raw = settings_cache.cached_get(db, SETTING_KEY)
    if not raw:
        return []
    try:
        hosts = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Ignoring unreadable %s value", SETTING_KEY)
        return []
    return [h for h in hosts if isinstance(h, str)] if isinstance(hosts, list) else []


def is_private_host_allowed(address_or_url: str, db: Session | None = None) -> bool:
    """True when the origin of ``address_or_url`` is on the allowlist.

    Opens its own session when ``db`` is None (the schema validator and the fetch path hold none).
    """
    parsed = urlparse(address_or_url)
    candidate = (
        f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else address_or_url
    )
    if db is not None:
        return any(same_source_origin(listed, candidate) for listed in get_private_hosts(db))
    from app.db.base import SessionLocal

    session = SessionLocal()
    try:
        return any(same_source_origin(listed, candidate) for listed in get_private_hosts(session))
    finally:
        session.close()
