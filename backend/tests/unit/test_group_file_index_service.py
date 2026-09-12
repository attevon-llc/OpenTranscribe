"""``group_file_index_service`` — the shared file-set derivation + dispatch helper.

Extracted from ``api/endpoints/groups.py``'s ``_reindex_group_shared_files`` so a
SCIM- or directory-driven membership change can dispatch the same
``update_file_access_index`` reindex the admin-UI membership endpoints already
trigger, instead of a stale ``accessible_user_ids`` surviving indefinitely on every
file shared with a group whose membership changed outside the groups UI.

No real database: both queries this module issues (``CollectionShare`` by
``target_group_id``, ``CollectionMember`` by ``collection_id``) are single filtered
selects, and the fake session below answers both exactly like the fakes already used
for ``idp_group_mapping_service`` (same file's docstring explains why a structural
stand-in is preferred over ~30 casts here).
"""

# mypy: disable-error-code="arg-type"
from __future__ import annotations

from app.models.media import CollectionMember
from app.models.sharing import CollectionShare
from app.services import group_file_index_service as svc


class _Result:
    """Applies ``Column == value`` / ``Column.in_(...)`` criteria over a row list."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *criteria):
        rows = self._rows
        for criterion in criteria:
            attr = getattr(getattr(criterion, "left", None), "key", None)
            if attr is None:
                continue
            op = getattr(criterion, "operator", None)
            op_name = getattr(op, "__name__", "")
            if op_name == "in_op":
                values = set(criterion.right.value)
                rows = [r for r in rows if getattr(r, attr, None) in values]
            else:
                value = getattr(getattr(criterion, "right", None), "value", None)
                rows = [r for r in rows if getattr(r, attr, None) == value]
        return _Result(rows)

    def distinct(self):
        # The real query selects a single column (``media_file_id``) and DISTINCTs
        # on it; dedupe on that same attribute here rather than by row identity.
        seen: set = set()
        deduped = []
        for row in self._rows:
            key = getattr(row, "media_file_id", row)
            if key not in seen:
                seen.add(key)
                deduped.append(row)
        return _Result(deduped)

    def all(self):
        return list(self._rows)


class FakeShare:
    def __init__(self, collection_id, target_group_id):
        self.collection_id = collection_id
        self.target_group_id = target_group_id


class FakeMember:
    def __init__(self, collection_id, media_file_id):
        self.collection_id = collection_id
        self.media_file_id = media_file_id


class FakeSession:
    def __init__(self, shares=(), members=()):
        self.shares = list(shares)
        self.members = list(members)

    def query(self, *entities):
        # Both callers query a single column (``CollectionShare.collection_id`` /
        # ``CollectionMember.media_file_id``), so `entities[0]` alone identifies
        # which table is being read.
        model = getattr(entities[0], "class_", None)
        if model is CollectionShare:
            return _Result(self.shares)
        if model is CollectionMember:
            return _Result(self.members)
        return _Result([])


class TestGetGroupSharedFileIds:
    def test_no_shared_collections_yields_no_files(self):
        assert svc.get_group_shared_file_ids(FakeSession(), group_id=7) == []

    def test_files_in_a_shared_collection_are_returned(self):
        db = FakeSession(
            shares=[FakeShare(collection_id=1, target_group_id=7)],
            members=[FakeMember(collection_id=1, media_file_id=101)],
        )
        assert svc.get_group_shared_file_ids(db, group_id=7) == [101]

    def test_a_collection_shared_with_a_different_group_is_excluded(self):
        db = FakeSession(
            shares=[FakeShare(collection_id=1, target_group_id=99)],
            members=[FakeMember(collection_id=1, media_file_id=101)],
        )
        assert svc.get_group_shared_file_ids(db, group_id=7) == []

    def test_files_are_deduplicated_across_several_shared_collections(self):
        db = FakeSession(
            shares=[
                FakeShare(collection_id=1, target_group_id=7),
                FakeShare(collection_id=2, target_group_id=7),
            ],
            members=[
                FakeMember(collection_id=1, media_file_id=101),
                FakeMember(collection_id=2, media_file_id=101),
                FakeMember(collection_id=2, media_file_id=102),
            ],
        )
        assert sorted(svc.get_group_shared_file_ids(db, group_id=7)) == [101, 102]


class TestReindexGroupSharedFiles:
    def test_dispatches_the_reindex_task_with_the_derived_file_ids(self, monkeypatch):
        dispatched: list[list[int]] = []
        monkeypatch.setattr(
            svc.update_file_access_index, "delay", lambda file_ids: dispatched.append(file_ids)
        )
        db = FakeSession(
            shares=[FakeShare(collection_id=1, target_group_id=7)],
            members=[FakeMember(collection_id=1, media_file_id=101)],
        )

        svc.reindex_group_shared_files(db, group_id=7)

        assert dispatched == [[101]]

    def test_a_group_sharing_nothing_dispatches_no_task(self, monkeypatch):
        """Must-fire control: a group with no shared collection is a real no-dispatch
        case, so this test cannot pass by dispatching unconditionally."""
        dispatched: list[list[int]] = []
        monkeypatch.setattr(
            svc.update_file_access_index, "delay", lambda file_ids: dispatched.append(file_ids)
        )

        svc.reindex_group_shared_files(FakeSession(), group_id=7)

        assert dispatched == []
