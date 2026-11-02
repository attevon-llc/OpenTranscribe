"""Tenant-stamped rows for the write-path tenancy suites, committed inside the test savepoint."""

from __future__ import annotations

import uuid

from app.models.tag import Tag
from app.models.watch_source import WatchSource
from fixtures import tag_world


def make_tag(db, *, name: str, user_id: int | None, org_id: int | None) -> Tag:
    """A tag of one tenant: ``org_id`` for an organization's, else the user's, else system.

    The row carries no origin, like a tag written before origins were recorded.
    """
    tag: Tag = tag_world.make_tag(db, name, user_id=user_id, org_id=org_id, source=None)
    return tag


def make_watch_source(
    db, *, user_id: int, org_id: int | None, names: list[str] | None = None
) -> WatchSource:
    """A local watch source stamped with a tenant, optionally carrying stored tag names."""
    source = WatchSource(
        name=f"ws-{uuid.uuid4().hex[:8]}",
        source_type="local",
        local_path="inbox",
        user_id=user_id,
        organization_id=org_id,
        tag_names=names,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source
