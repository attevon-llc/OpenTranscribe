"""An organization world for the content-governance suites (#1275-#1277).

``gov`` builds one organization with an admin, two ordinary members and an outsider, plus a
second organization, and offers builders for files and collections and ``acting_as`` to run
HTTP requests as one of them in a chosen tenant (the two auth dependencies are overridden, the
shape ``test_collection_tenancy.py`` uses). Everything is committed inside the test's
savepoint harness, so nothing outlives the test. Opt-in; nothing autouse.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.user import User


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def new_user(db: Session, label: str) -> User:
    user = User(
        email=f"gov_{label}_{_tag()}@example.com",
        full_name=f"gov {label}",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@dataclass
class GovWorld:
    """Handles to the people and the builders a governance test needs."""

    db: Session
    org: Organization
    other_org: Organization
    admin: User
    alice: User
    bob: User
    outsider: User

    def user(self, label: str) -> User:
        return new_user(self.db, label)

    def join(self, user: User, org: Organization | None = None, role: str = "org:member") -> None:
        self.db.add(
            OrganizationMembership(organization_id=(org or self.org).id, user_id=user.id, role=role)
        )
        self.db.commit()

    def file(self, owner: User, org: Organization | None | bool = True, **fields: Any) -> MediaFile:
        """A completed file; ``org=True`` (default) puts it in the world's organization."""
        org_id = self.org.id if org is True else (org.id if org else None)
        file_uuid = uuid.uuid4()
        row = MediaFile(
            **{
                "uuid": file_uuid,
                "filename": f"gov_{file_uuid.hex[:8]}.mp3",
                "storage_path": f"gov/{file_uuid}.mp3",
                "content_type": "audio/mpeg",
                "file_size": 1,
                "user_id": owner.id,
                "organization_id": org_id,
                "status": "completed",
                **fields,
            }
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def collection(
        self,
        creator: User | None,
        name: str | None = None,
        org: Organization | None | bool = True,
        **fields: Any,
    ) -> Collection:
        org_id = self.org.id if org is True else (org.id if org else None)
        row = Collection(
            name=name or f"gov coll {_tag()}",
            user_id=creator.id if creator else None,
            organization_id=org_id,
            **fields,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def put(self, collection: Collection, *files: MediaFile) -> None:
        for media_file in files:
            self.db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
        self.db.commit()

    def share(
        self,
        collection: Collection,
        target: User,
        permission: str = "viewer",
        by: User | None = None,
    ) -> None:
        self.db.add(
            CollectionShare(
                collection_id=collection.id,
                shared_by_id=(by or self.alice).id,
                target_type="user",
                target_user_id=target.id,
                permission=permission,
            )
        )
        self.db.commit()

    @contextmanager
    def acting_as(
        self, user: User, org: Organization | None | bool = True, role: str | None = None
    ) -> Iterator[None]:
        """Run requests as ``user`` in the tenant ``org`` (``None`` = personal workspace)."""
        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_base_context
        from app.api.deps_context import get_current_context
        from app.api.endpoints.auth import get_current_active_user
        from app.api.endpoints.auth import get_current_user
        from app.main import app

        org_id = self.org.id if org is True else (org.id if org else None)
        if role is None:
            role = {self.admin.id: "org:admin"}.get(user.id, "org:member")
        app.dependency_overrides[get_current_active_user] = lambda: user
        # A few routes (the delete / cancel-upload route) depend on the bare user dependency.
        app.dependency_overrides[get_current_user] = lambda: user
        context = RequestContext(
            user=user, org_id=org_id, org_role=role if org_id is not None else None
        )
        # ``require_org_admin`` hangs off the base context, not the full one.
        app.dependency_overrides[get_base_context] = lambda: context
        app.dependency_overrides[get_current_context] = lambda: context
        try:
            yield
        finally:
            app.dependency_overrides.pop(get_current_active_user, None)
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(get_base_context, None)
            app.dependency_overrides.pop(get_current_context, None)


@pytest.fixture()
def gov(db_session) -> GovWorld:
    """Org ``org``: ``admin`` (org:admin), ``alice`` and ``bob`` (members); ``outsider`` joins none."""
    db = db_session

    def _org(label: str) -> Organization:
        org = Organization(
            external_org_id=f"org_gov_{label}_{_tag()}", name=f"gov {label}", is_active=True
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        return org

    world = GovWorld(
        db=db,
        org=_org("a"),
        other_org=_org("b"),
        admin=new_user(db, "admin"),
        alice=new_user(db, "alice"),
        bob=new_user(db, "bob"),
        outsider=new_user(db, "outsider"),
    )
    world.join(world.admin, role="org:admin")
    world.join(world.alice)
    world.join(world.bob)
    return world
