"""Shared rows and request-context plumbing for the tag characterization suites.

``acting_as`` runs requests as a chosen user in a chosen tenant by overriding the two
auth dependencies, the same shape ``tests/api/test_tag_tenancy.py`` uses, so the org
context is exactly the one a test names.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.user import User
from app.services.tag_service import normalize_tag_name


def word(prefix: str = "tw") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid.uuid4().hex[:8]}@example.com",
        full_name=f"{label} user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_org(db, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid.uuid4().hex[:8]}",
        name=f"{label} Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def join(db, org: Organization, user: User, role: str = "org:member") -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db.commit()


def make_file(db, owner: User, org_id: int | None = None) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        filename=f"tagchar_{str(file_uuid)[:8]}.mp3",
        storage_path=f"tagchar/{file_uuid}.mp3",
        content_type="audio/mpeg",
        file_size=1,
        user_id=owner.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def make_tag(
    db,
    name: str,
    *,
    user_id: int | None,
    org_id: int | None = None,
    source: str | None = "manual",
) -> Tag:
    tag = Tag(
        name=name,
        user_id=user_id,
        organization_id=org_id,
        source=source,
        normalized_name=normalize_tag_name(name),
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def attach(db, media_file: MediaFile, tag: Tag, *, source: str | None = "manual") -> FileTag:
    link = FileTag(media_file_id=media_file.id, tag_id=tag.id, source=source)
    db.add(link)
    db.commit()
    return link


@contextmanager
def acting_as(user: User, org_id: int | None, org_role: str | None = None):
    """Run requests as ``user`` in tenant ``org_id`` (None = personal workspace)."""
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app
    from app.services.request_context import RequestContext

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role=org_role if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


def build_org_world(db):
    """One org with an admin (alice) and a member (bob); alice also joins a second org."""
    org_a = make_org(db, "A")
    org_b = make_org(db, "B")
    alice = make_user(db, "alice")
    bob = make_user(db, "bob")
    join(db, org_a, alice, role="org:admin")
    join(db, org_a, bob)
    join(db, org_b, alice)
    return SimpleNamespace(db=db, org_a=org_a, org_b=org_b, alice=alice, bob=bob)
