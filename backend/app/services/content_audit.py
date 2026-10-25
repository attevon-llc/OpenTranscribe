"""The one write path for content-access and governance audit events (issue #1277).

Every emit site (file view, stream URL, download, export, collection membership, visibility,
holds) calls :func:`record_content_event`, so the rules below exist once:

* **Tenant scope comes from the RESOURCE**, never the actor. ``organization_id`` is the
  resource's org and ``tenant_scope`` is ``org`` or ``personal``; ``build_org_scope_clause``
  uses it so a member's own-file event is never readable by their org admins (finding F5).
  ``target_user_id`` is the resource owner when that is not the actor, so "who looked at
  Alice's files" is one filter.
* **High-volume events are opt-in.** ``content.file.view``, ``content.media.access`` and
  ``content.export`` are recorded only when the ``audit.content_access`` capability AND the
  super_admin ``audit.content_access.enabled`` setting (default off) are both true.
  Governance events (membership, visibility, holds) are always recorded: they are low volume
  and the features require them.
* **View-class events are de-duplicated** (``content.file.view`` and a ``stream_url`` media
  access): once per ``(event, actor, resource, surface)`` per ``CONTENT_AUDIT_DEDUP_SECONDS``,
  with ``SET NX EX`` in Redis. **Redis being unavailable means the event is written**: the
  failure mode is more audit, never less. Downloads and exports are never de-duplicated; each is
  a distinct copy leaving the system.
* **Never in an event:** transcript text, filenames, titles, collection names or hold reasons.
  ``details`` carries ids, kinds, counts and formats only (``test_content_audit_events`` pins it).
* **It never raises.** An audit failure must not fail the request it describes.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from app.auth.audit import TENANT_SCOPE_ORG
from app.auth.audit import TENANT_SCOPE_PERSONAL
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.capabilities import capability_enabled
from app.core.constants import AUDIT_CONTENT_ACCESS_DEFAULT
from app.core.constants import AUDIT_CONTENT_ACCESS_SETTING_KEY
from app.core.constants import CONTENT_AUDIT_DEDUP_SECONDS

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.orm import Session

    from app.models.user import User
    from app.services.platform_bypass import PlatformBypass

logger = logging.getLogger(__name__)

CONTENT_ACCESS_CAPABILITY = "audit.content_access"

#: Recorded only while content auditing is on (capability AND setting).
GATED_EVENTS = frozenset(
    {
        AuditEventType.CONTENT_FILE_VIEW,
        AuditEventType.CONTENT_MEDIA_ACCESS,
        AuditEventType.CONTENT_EXPORT,
    }
)

#: ``details.kind`` of a ``content.media.access`` event. A stream URL is collapsed within the
#: window (a player re-requests one constantly); the two download kinds never are, since each
#: is a copy of the media leaving.
KIND_STREAM_URL = "stream_url"
KIND_DOWNLOAD_PREPARE = "download_prepare"
KIND_DOWNLOAD_STREAM = "download_stream"
_DEDUP_MEDIA_KINDS = frozenset({KIND_STREAM_URL})

#: Most file uuids a membership event lists; the count is always exact.
MAX_MEMBERSHIP_UUIDS = 100


@dataclass(frozen=True)
class AuditedResource:
    """What an event is about, reduced to the three facts an audit record may keep."""

    resource_type: str
    uuid: str
    organization_id: int | None
    owner_user_id: int | None

    @classmethod
    def of(cls, resource_type: str, row: Any) -> AuditedResource:
        """From any row with ``uuid``, ``organization_id`` and an owner/creator ``user_id``."""
        org = getattr(row, "organization_id", None)
        owner = getattr(row, "user_id", None)
        return cls(
            resource_type=resource_type,
            uuid=str(row.uuid),
            organization_id=int(org) if org is not None else None,
            owner_user_id=int(owner) if owner is not None else None,
        )

    @classmethod
    def file(cls, row: Any) -> AuditedResource:
        return cls.of("media_file", row)

    @classmethod
    def collection(cls, row: Any) -> AuditedResource:
        return cls.of("collection", row)

    @classmethod
    def conversation(cls, row: Any) -> AuditedResource:
        return cls.of("conversation", row)


def content_audit_enabled(db: Session, request: Request | None = None) -> bool:
    """True when the view / media-access / export events are being recorded.

    Both gates must be open: the deployment's ``audit.content_access`` capability (a derived
    deployment's resolver may withhold it) and the super_admin setting, which defaults to
    OFF so a deployment opts in from the admin UI with no environment variable.
    """
    from app.services.system_settings_service import get_setting_bool

    if not capability_enabled(CONTENT_ACCESS_CAPABILITY, request):
        return False
    return get_setting_bool(db, AUDIT_CONTENT_ACCESS_SETTING_KEY, AUDIT_CONTENT_ACCESS_DEFAULT)


def _dedup_key(event: AuditEventType, actor_id: int, resource_uuid: str, surface: str) -> str:
    raw = f"{event.value}|{actor_id}|{resource_uuid}|{surface}"
    return f"audit:dedup:{hashlib.sha256(raw.encode()).hexdigest()}"


def _already_recorded(
    event: AuditEventType, actor_id: int, resource_uuid: str, surface: str
) -> bool:
    """True when an identical view-class event was written inside the window.

    ``SET NX EX`` claims the window atomically; the loser of the race (or a later repeat)
    sees it taken. Any Redis failure answers False: write the event.
    """
    try:
        from app.core.redis import get_probe_redis

        claimed = get_probe_redis().set(
            _dedup_key(event, actor_id, resource_uuid, surface),
            "1",
            nx=True,
            ex=CONTENT_AUDIT_DEDUP_SECONDS,
        )
        return not claimed
    except Exception as exc:  # noqa: BLE001
        logger.debug("content-audit de-dup unavailable, writing the event: %s", exc)
        return False


def _is_view_class(event: AuditEventType, details: dict[str, Any]) -> bool:
    if event is AuditEventType.CONTENT_FILE_VIEW:
        return True
    return (
        event is AuditEventType.CONTENT_MEDIA_ACCESS and details.get("kind") in _DEDUP_MEDIA_KINDS
    )


def record_content_event(
    db: Session,
    request: Request | None,
    *,
    event: AuditEventType,
    actor: User,
    resource: AuditedResource,
    details: dict[str, Any] | None = None,
    dedup: bool = True,
    bypass: PlatformBypass | None = None,
) -> bool:
    """Write one content or governance audit event; return whether it was written.

    Args:
        db: A session, used only to read the ``audit.content_access`` setting.
        request: The HTTP request, for the client address and user agent (None is allowed).
        event: The event type. Members of :data:`GATED_EVENTS` are dropped while content
            auditing is off; every other type is always recorded.
        actor: Who did it.
        resource: What it was done to; its organization decides ``organization_id`` and
            ``tenant_scope``.
        details: Ids, kinds, counts and formats. Never names, titles, text or reasons.
        dedup: Collapse a repeat view-class event inside ``CONTENT_AUDIT_DEDUP_SECONDS``.
            Ignored for downloads, exports and every governance event.
        bypass: The request's platform bypass; under a support-access grant its uuid is
            recorded as ``details.grant_uuid`` so a tenant can tell staff access from its own.
    """
    try:
        body = dict(details or {})
        if event in GATED_EVENTS and not content_audit_enabled(db, request):
            return False
        if dedup and _is_view_class(event, body):
            surface = str(body.get("surface") or body.get("kind") or "")
            if _already_recorded(event, int(actor.id), resource.uuid, surface):
                return False
        if bypass is not None and bypass.grant is not None:
            body["grant_uuid"] = bypass.grant.uuid

        from app.middleware.audit import get_request_context

        ctx = get_request_context(request) if request is not None else {}
        owner = resource.owner_user_id
        audit_logger.log(
            event_type=event,
            outcome=AuditOutcome.SUCCESS,
            user_id=int(actor.id),
            username=str(actor.email),
            source_ip=ctx.get("source_ip"),
            user_agent=ctx.get("user_agent"),
            organization_id=resource.organization_id,
            target_user_id=owner if owner is not None and owner != actor.id else None,
            resource_type=resource.resource_type,
            resource_uuid=resource.uuid,
            tenant_scope=(
                TENANT_SCOPE_ORG if resource.organization_id is not None else TENANT_SCOPE_PERSONAL
            ),
            details=body,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning("content-audit write failed (request unaffected)", exc_info=True)
        return False


def record_file_event(
    db: Session,
    request: Request | None,
    *,
    event: AuditEventType,
    actor: User,
    file: Any,
    details: dict[str, Any] | None = None,
    dedup: bool = True,
    bypass: PlatformBypass | None = None,
) -> bool:
    """:func:`record_content_event` for a media file, given its row or its uuid.

    A uuid is resolved to the row only AFTER the opt-in gate, so a deployment with content
    auditing off pays no extra query on the file page. ``file`` is typed ``Any`` because both
    a ``MediaFile`` and a uuid (``str`` / ``UUID``) are legitimate.
    """
    try:
        if event in GATED_EVENTS and not content_audit_enabled(db, request):
            return False
        row = file
        if not hasattr(file, "organization_id"):
            from app.models.media import MediaFile

            row = db.query(MediaFile).filter(MediaFile.uuid == str(file)).first()
            if row is None:
                return False
        return record_content_event(
            db,
            request,
            event=event,
            actor=actor,
            resource=AuditedResource.file(row),
            details=details,
            dedup=dedup,
            bypass=bypass,
        )
    except Exception:  # noqa: BLE001
        logger.warning("content-audit write failed (request unaffected)", exc_info=True)
        return False
