"""Recorders and share helpers the tag suites have in common.

The tag services end every mutation in ``on_tags_changed``, which enqueues
``update_file_tags_index``; these stand-ins capture both calls without a broker. The share
helper grants access to one file through a single-file collection, the only file-share
path the permission service knows.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.sharing import CollectionShare


class ReindexRecorder:
    """Stands in for the Celery task; every ``delay`` call is kept as a list of ids."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def delay(self, file_ids: Any) -> None:
        self.calls.append(list(file_ids))


def record_reindex(monkeypatch) -> ReindexRecorder:
    """Replace ``update_file_tags_index`` where ``on_tags_changed`` looks it up."""
    from app.tasks import search_file_field_tasks

    recorder = ReindexRecorder()
    monkeypatch.setattr(search_file_field_tasks, "update_file_tags_index", recorder)
    return recorder


class HookRecorder:
    """Stands in for ``on_tags_changed``; each call is kept as a dict of its arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, db, file_ids=None, *, user_id=None, system_scope=False) -> list[int]:
        files = [int(fid) for fid in (file_ids or [])]
        self.calls.append({"files": files, "user_id": user_id, "system_scope": system_scope})
        return files


def record_hook(monkeypatch, module_path: str) -> HookRecorder:
    """Replace the ``on_tags_changed`` name bound in ``module_path``."""
    recorder = HookRecorder()
    monkeypatch.setattr(f"{module_path}.on_tags_changed", recorder)
    return recorder


def count_commits(monkeypatch, db) -> list[int]:
    """Wrap ``db.commit`` so a test can tell whether (and how often) a handler ran it.

    The auth dependencies end their read transaction with a commit
    (``end_read_transaction``); that is connection hygiene, not a write, so it is not counted.
    """
    seen: list[int] = []
    real_commit = db.commit

    def commit_and_count():
        if sys._getframe(1).f_code.co_name != "end_read_transaction":
            seen.append(1)
        return real_commit()

    monkeypatch.setattr(db, "commit", commit_and_count)
    return seen


def share_file(db, owner, target, media_file, permission: str = "viewer") -> CollectionShare:
    """Give ``target`` ``permission`` on ``media_file`` via a one-file collection of ``owner``."""
    collection = Collection(name=f"share-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.add(collection)
    db.flush()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    grant = CollectionShare(
        collection_id=collection.id,
        shared_by_id=owner.id,
        target_type="user",
        target_user_id=target.id,
        permission=permission,
    )
    db.add(grant)
    db.commit()
    return grant
