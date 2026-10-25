"""Read-time helpers for the organization audit screen (issue #1277).

An audit event stores ids only: no name, title or text (``services/content_audit``). The
organization admin's screen needs something a person can read, so the labels are resolved
HERE, at read time, from Postgres and only for what the organization may know:

* an actor or target is named only while they are still a MEMBER of the organization; a person
  who left, or who was never a member (a platform operator, a failed login), stays an id;
* a file or collection is titled only when its row belongs to THIS organization (a collection also
  only when the viewing admin may see it), so an event
  that names another tenant's resource, a personal one or a deleted one gets no label.

The label is therefore never stored, never exported from another tenant, and disappears with
the row it describes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.organization import OrganizationMembership
from app.models.user import User
from app.services.collection_visibility import visible_collections_by_uuid

RESOURCE_MEDIA_FILE = "media_file"
RESOURCE_COLLECTION = "collection"


def member_id_for_uuid(db: Session, org_id: int, user_uuid: UUID | str) -> int | None:
    """The user id behind ``user_uuid`` if that user is a member of ``org_id``, else ``None``.

    An unknown uuid and a non-member's uuid answer the same, so the caller's refusal is not an
    oracle for who exists on the platform.
    """
    row = (
        db.query(User.id)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(OrganizationMembership.organization_id == org_id, User.uuid == str(user_uuid))
        .first()
    )
    return int(row[0]) if row else None


def _member_labels(db: Session, org_id: int, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = (
        db.query(User.id, User.full_name, User.email)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(OrganizationMembership.organization_id == org_id, User.id.in_(user_ids))
        .all()
    )
    return {int(r[0]): str(r[1] or r[2]) for r in rows}


def _resource_labels(
    db: Session, org_id: int, viewer_id: int, wanted: dict[str, set[str]]
) -> dict[tuple[str, str], str]:
    labels: dict[tuple[str, str], str] = {}
    if wanted.get(RESOURCE_MEDIA_FILE):
        for row in db.query(MediaFile.uuid, MediaFile.title, MediaFile.filename).filter(
            MediaFile.organization_id == org_id,
            MediaFile.uuid.in_(wanted[RESOURCE_MEDIA_FILE]),
        ):
            labels[(RESOURCE_MEDIA_FILE, str(row[0]))] = str(row[1] or row[2] or "")
    if wanted.get(RESOURCE_COLLECTION):
        # Through the one visibility statement: a collection the viewer may not see (a restricted
        # one they are not named on) is as absent here as it is everywhere else.
        for collection in visible_collections_by_uuid(
            db, viewer_id, org_id, wanted[RESOURCE_COLLECTION]
        ):
            labels[(RESOURCE_COLLECTION, str(collection.uuid))] = str(collection.name)
    return labels


def with_display_fields(
    db: Session, org_id: int, viewer_id: int, logs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Copies of ``logs`` with ``actor_display``, ``target_display`` and ``resource_display``.

    ``viewer_id`` is the administrator reading: a collection they may not see is not titled.
    Each is ``None`` when it cannot be resolved inside ``org_id``. Three queries at most,
    however many events there are.
    """
    people = {
        int(v)
        for log in logs
        for v in (log.get("user_id"), log.get("target_user_id"))
        if v is not None
    }
    wanted: dict[str, set[str]] = {}
    for log in logs:
        kind, ident = log.get("resource_type"), log.get("resource_uuid")
        if kind in (RESOURCE_MEDIA_FILE, RESOURCE_COLLECTION) and ident:
            wanted.setdefault(str(kind), set()).add(str(ident))

    names = _member_labels(db, org_id, people)
    titles = _resource_labels(db, org_id, viewer_id, wanted)

    enriched = []
    for log in logs:
        actor, target = log.get("user_id"), log.get("target_user_id")
        enriched.append(
            {
                **log,
                "actor_display": names.get(int(actor)) if actor is not None else None,
                "target_display": names.get(int(target)) if target is not None else None,
                "resource_display": titles.get(
                    (str(log.get("resource_type")), str(log.get("resource_uuid")))
                ),
            }
        )
    return enriched
