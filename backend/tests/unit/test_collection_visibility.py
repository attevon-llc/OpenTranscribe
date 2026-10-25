"""The one collection-visibility rule shared by the file page, search cards and the search filter.

``services/collection_visibility`` exists so the surfaces that NAME a collection on a file
cannot drift apart, and so issue #1275's restricted collections replace one predicate rather
than every raw ``Collection`` join. These tests pin what each function admits and refuses,
that the batch costs a constant number of queries for a whole result page, and that the file
page now answers through the same function.
"""

from __future__ import annotations

import uuid as uuid_pkg

from sqlalchemy import event

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.user import User
from app.services.collection_visibility import NO_VISIBLE_COLLECTION
from app.services.collection_visibility import collections_on_files
from app.services.collection_visibility import resolve_collection_filter
from app.services.collection_visibility import visible_collection_ids


def _user(db, label: str) -> User:
    user = User(
        email=f"collvis_{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _file(db, owner: User, org_id: int | None = None) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=fuuid,
        filename=f"collvis_{str(fuuid)[:8]}.mp3",
        storage_path=f"collvis/{fuuid}.mp3",
        content_type="audio/mpeg",
        file_size=1,
        user_id=owner.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _collection(db, name: str, owner: User | None, org_id: int | None = None) -> Collection:
    row = Collection(
        name=f"{name} {uuid_pkg.uuid4().hex[:6]}",
        user_id=owner.id if owner else None,
        organization_id=org_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _put(db, collection: Collection, *files: MediaFile) -> None:
    for media_file in files:
        db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.commit()


def _share(db, collection: Collection, owner: User, target: User) -> None:
    db.add(
        CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=target.id,
            permission="viewer",
        )
    )
    db.commit()


def _org(db) -> Organization:
    org = Organization(
        external_org_id=f"org_collvis_{uuid_pkg.uuid4().hex[:8]}",
        name="Collvis Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _join(db, org: Organization, user: User, role: str = "org:member") -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db.commit()


class TestCollectionsOnFiles:
    def test_names_the_callers_collections_sorted_by_name(self, db_session):
        alice = _user(db_session, "alice")
        media = _file(db_session, alice)
        zeta = _collection(db_session, "Zeta", alice)
        alpha = _collection(db_session, "Alpha", alice)
        _put(db_session, zeta, media)
        _put(db_session, alpha, media)

        named = collections_on_files(db_session, [media.id], alice.id, organization_id=None)

        assert [c.id for c in named[media.id]] == [alpha.id, zeta.id]

    def test_another_users_collection_holding_the_file_is_never_named(self, db_session):
        alice = _user(db_session, "alice")
        mallory = _user(db_session, "mallory")
        media = _file(db_session, alice)
        mine = _collection(db_session, "Mine", alice)
        theirs = _collection(db_session, "Theirs", mallory)
        _put(db_session, mine, media)
        _put(db_session, theirs, media)

        named = collections_on_files(db_session, [media.id], alice.id, organization_id=None)

        assert [c.id for c in named[media.id]] == [mine.id]

    def test_a_file_in_no_visible_collection_is_absent(self, db_session):
        alice = _user(db_session, "alice")
        mallory = _user(db_session, "mallory")
        media = _file(db_session, alice)
        _put(db_session, _collection(db_session, "Theirs", mallory), media)

        assert collections_on_files(db_session, [media.id], alice.id, organization_id=None) == {}

    def test_an_org_collection_is_named_for_a_member_and_not_for_an_outsider(self, db_session):
        alice = _user(db_session, "alice")
        bob = _user(db_session, "bob")
        outsider = _user(db_session, "outsider")
        org = _org(db_session)
        _join(db_session, org, alice, "org:admin")
        _join(db_session, org, bob)
        media = _file(db_session, alice, org.id)
        board = _collection(db_session, "Board", alice, org.id)
        _put(db_session, board, media)

        for_bob = collections_on_files(db_session, [media.id], bob.id, organization_id=org.id)
        for_outsider_in_org = collections_on_files(
            db_session, [media.id], outsider.id, organization_id=org.id
        )
        for_outsider_personal = collections_on_files(
            db_session, [media.id], outsider.id, organization_id=None
        )

        assert [c.id for c in for_bob[media.id]] == [board.id]
        assert for_outsider_in_org == {}
        assert for_outsider_personal == {}

    def test_unscoped_uses_each_files_own_tenant(self, db_session):
        alice = _user(db_session, "alice")
        org = _org(db_session)
        _join(db_session, org, alice)
        personal_file = _file(db_session, alice)
        org_file = _file(db_session, alice, org.id)
        personal = _collection(db_session, "Personal", alice)
        org_coll = _collection(db_session, "Org", alice, org.id)
        _put(db_session, personal, personal_file, org_file)
        _put(db_session, org_coll, personal_file, org_file)

        named = collections_on_files(db_session, [personal_file.id, org_file.id], alice.id)

        assert [c.id for c in named[personal_file.id]] == [personal.id]
        assert [c.id for c in named[org_file.id]] == [org_coll.id]

    def test_a_whole_page_costs_the_same_queries_as_one_file(self, db_session):
        """No N+1: the search card asks for 20 files at once."""
        alice = _user(db_session, "alice")
        files = [_file(db_session, alice) for _ in range(6)]
        for media_file in files:
            _put(db_session, _collection(db_session, "Per file", alice), media_file)
        # Read every id now: after a commit the rows are expired, and a lazy refresh inside
        # the counted window would charge the first call a query the second does not pay.
        alice_id = alice.id
        file_ids = [f.id for f in files]

        def count_queries(ids: list[int]) -> tuple[int, dict]:
            statements: list[str] = []

            def _record(_conn, _cursor, statement, *_args):
                statements.append(statement)

            engine = db_session.get_bind()
            event.listen(engine, "before_cursor_execute", _record)
            try:
                result = collections_on_files(db_session, ids, alice_id, organization_id=None)
            finally:
                event.remove(engine, "before_cursor_execute", _record)
            return len(statements), result

        one, one_result = count_queries(file_ids[:1])
        many, many_result = count_queries(file_ids)

        assert len(one_result) == 1
        assert len(many_result) == 6
        assert many == one

    def test_the_file_page_names_exactly_what_the_search_card_names(self, db_session):
        from app.api.endpoints.files.crud import get_file_collections

        alice = _user(db_session, "alice")
        mallory = _user(db_session, "mallory")
        media = _file(db_session, alice)
        mine = _collection(db_session, "Mine", alice)
        shared_in = _collection(db_session, "Shared in", mallory)
        _put(db_session, mine, media)
        _put(db_session, shared_in, media)
        _share(db_session, shared_in, mallory, alice)

        page = {c["uuid"] for c in get_file_collections(db_session, media.id, alice.id, None)}
        card = {
            str(c.uuid)
            for c in collections_on_files(db_session, [media.id], alice.id, None)[media.id]
        }

        assert page == card == {str(mine.uuid)}


class TestResolveCollectionFilter:
    def test_no_filter_asked_for_is_none(self, db_session):
        alice = _user(db_session, "alice")
        assert resolve_collection_filter(db_session, user_id=alice.id, organization_id=None) is None

    def test_an_own_collection_resolves_by_uuid_and_by_id(self, db_session):
        alice = _user(db_session, "alice")
        mine = _collection(db_session, "Mine", alice)

        by_uuid = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=None, collection_uuid=mine.uuid
        )
        by_id = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=None, collection_id=mine.id
        )

        assert by_uuid == mine.id
        assert by_id == mine.id

    def test_a_collection_shared_with_the_caller_is_filterable(self, db_session):
        """The sidebar lists `ownership=all`, shared collections included."""
        alice = _user(db_session, "alice")
        mallory = _user(db_session, "mallory")
        theirs = _collection(db_session, "Theirs", mallory)
        _share(db_session, theirs, mallory, alice)

        resolved = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=None, collection_uuid=theirs.uuid
        )

        assert resolved == theirs.id

    def test_a_hidden_or_unknown_collection_filters_to_nothing(self, db_session):
        alice = _user(db_session, "alice")
        mallory = _user(db_session, "mallory")
        private = _collection(db_session, "Private", mallory)

        hidden_uuid = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=None, collection_uuid=private.uuid
        )
        hidden_id = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=None, collection_id=private.id
        )
        unknown = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=None, collection_uuid=uuid_pkg.uuid4()
        )

        assert hidden_uuid == hidden_id == unknown == NO_VISIBLE_COLLECTION

    def test_an_own_collection_of_another_tenant_filters_to_nothing(self, db_session):
        alice = _user(db_session, "alice")
        org = _org(db_session)
        _join(db_session, org, alice)
        org_coll = _collection(db_session, "Org", alice, org.id)

        in_personal = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=None, collection_uuid=org_coll.uuid
        )
        in_org = resolve_collection_filter(
            db_session, user_id=alice.id, organization_id=org.id, collection_uuid=org_coll.uuid
        )

        assert in_personal == NO_VISIBLE_COLLECTION
        assert in_org == org_coll.id
        assert visible_collection_ids(db_session, alice.id, org.id) == {org_coll.id}


class TestSearchCollectionFilterUsesLiveMembership:
    """The search collection filter used the chunk documents' ``collection_ids``, which is
    written only at index time: a file added to a collection afterwards (every AI
    auto-collection, every "add to collection") never matched its own collection's filter.
    Seen on the dev stack: four files, each in two collections, every chunk
    ``collection_ids: []``, and the filter returned an empty page."""

    @staticmethod
    def _filters(monkeypatch, db_session, collection_id: int) -> list[dict]:
        from contextlib import contextmanager

        from app.services.search.hybrid_search_service import HybridSearchService

        @contextmanager
        def _test_session():
            yield db_session

        monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)
        return HybridSearchService()._build_filters(
            7, None, None, None, None, collection_id=collection_id
        )

    def test_filters_by_the_collections_current_files(self, monkeypatch, db_session):
        alice = _user(db_session, "alice")
        inside = _file(db_session, alice)
        _file(db_session, alice)  # outside the collection
        coll = _collection(db_session, "Board", alice)
        _put(db_session, coll, inside)

        filters = self._filters(monkeypatch, db_session, coll.id)

        assert {"terms": {"file_uuid": [str(inside.uuid)]}} in filters
        assert not any("collection_ids" in str(clause) for clause in filters)

    def test_a_hidden_collection_matches_nothing(self, monkeypatch, db_session):
        filters = self._filters(monkeypatch, db_session, NO_VISIBLE_COLLECTION)
        assert {"terms": {"file_uuid": []}} in filters

    def test_membership_past_the_cap_is_reported_not_truncated(self, db_session):
        from app.services.collection_visibility import collection_member_file_uuids

        alice = _user(db_session, "alice")
        coll = _collection(db_session, "Big", alice)
        _put(db_session, coll, _file(db_session, alice), _file(db_session, alice))

        assert collection_member_file_uuids(db_session, coll.id, limit=1) is None
        assert len(collection_member_file_uuids(db_session, coll.id, limit=2) or []) == 2
