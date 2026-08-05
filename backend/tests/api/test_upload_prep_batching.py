"""Issue #284 A2.8 — upload prep resolves collections and tags in bulk.

``add_file_to_collections`` and ``add_tags_to_file`` used to issue two queries per
named collection / tag (a lookup plus a membership check). The count is bounded by
what the client sent, but this is the upload-prep path — the SPA calls it once per
file — so a multi-file upload into a handful of collections multiplied the round
trips out fast.

These tests count the SELECTs actually emitted and pin the resulting rows, so a
future refactor cannot quietly restore the per-item loop or change what gets linked.
"""

import uuid as uuid_mod
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.api.endpoints.files.prepare_upload import add_file_to_collections
from app.api.endpoints.files.prepare_upload import add_tags_to_file
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag


@contextmanager
def count_selects(session):
    """Count SELECT statements emitted on the session's connection."""
    counter = {"selects": 0}
    connection = session.connection()

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["selects"] += 1

    event.listen(connection.engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield counter
    finally:
        event.remove(connection.engine, "before_cursor_execute", before_cursor_execute)


def _make_media_file(db_session, user) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="batch.mp4",
        storage_path="test/batch.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _make_collections(db_session, user, count: int) -> list[Collection]:
    collections = []
    for index in range(count):
        collection = Collection(
            uuid=str(uuid_mod.uuid4()),
            user_id=user.id,
            name=f"Batch collection {index} {uuid_mod.uuid4().hex[:8]}",
        )
        db_session.add(collection)
        collections.append(collection)
    db_session.flush()
    return collections


class TestAddFileToCollections:
    def test_resolution_is_batched(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user)
        collections = _make_collections(db_session, normal_user, 6)
        uuids = [uuid_mod.UUID(str(c.uuid)) for c in collections]

        with count_selects(db_session) as counter:
            add_file_to_collections(db_session, media_file.id, normal_user.id, uuids)

        # One lookup for the collections, one for existing membership.
        assert counter["selects"] == 2, (
            f"expected 2 SELECTs for 6 collections, got {counter['selects']} — "
            "the per-collection loop is back (issue #284 A2.8)"
        )

        members = (
            db_session.query(CollectionMember)
            .filter(CollectionMember.media_file_id == media_file.id)
            .all()
        )
        assert {m.collection_id for m in members} == {c.id for c in collections}

    def test_unknown_and_foreign_collections_are_skipped(self, db_session, normal_user, admin_user):
        media_file = _make_media_file(db_session, normal_user)
        mine = _make_collections(db_session, normal_user, 1)[0]
        theirs = _make_collections(db_session, admin_user, 1)[0]

        add_file_to_collections(
            db_session,
            media_file.id,
            normal_user.id,
            [
                uuid_mod.UUID(str(mine.uuid)),
                uuid_mod.UUID(str(theirs.uuid)),
                uuid_mod.uuid4(),  # does not exist at all
            ],
        )

        members = (
            db_session.query(CollectionMember)
            .filter(CollectionMember.media_file_id == media_file.id)
            .all()
        )
        assert [m.collection_id for m in members] == [mine.id]

    def test_repeated_calls_do_not_duplicate_membership(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user)
        collection = _make_collections(db_session, normal_user, 1)[0]
        uuids = [uuid_mod.UUID(str(collection.uuid))]

        add_file_to_collections(db_session, media_file.id, normal_user.id, uuids)
        add_file_to_collections(db_session, media_file.id, normal_user.id, uuids)

        count = (
            db_session.query(CollectionMember)
            .filter(
                CollectionMember.media_file_id == media_file.id,
                CollectionMember.collection_id == collection.id,
            )
            .count()
        )
        assert count == 1

    def test_a_repeated_uuid_in_one_call_links_once(self, db_session, normal_user):
        """`collection_member` has no unique constraint — dedupe in code."""
        media_file = _make_media_file(db_session, normal_user)
        collection = _make_collections(db_session, normal_user, 1)[0]
        coll_uuid = uuid_mod.UUID(str(collection.uuid))

        add_file_to_collections(db_session, media_file.id, normal_user.id, [coll_uuid, coll_uuid])

        count = (
            db_session.query(CollectionMember)
            .filter(
                CollectionMember.media_file_id == media_file.id,
                CollectionMember.collection_id == collection.id,
            )
            .count()
        )
        assert count == 1

    def test_empty_list_is_a_no_op(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user)
        with count_selects(db_session) as counter:
            add_file_to_collections(db_session, media_file.id, normal_user.id, [])
        assert counter["selects"] == 0


class TestAddTagsToFile:
    def test_query_count_does_not_scale_with_the_number_of_tags(self, db_session, normal_user):
        """Five tags and twenty tags must cost the same number of SELECTs."""
        counts = {}
        for tag_count in (5, 20):
            media_file = _make_media_file(db_session, normal_user)
            names = [f"batch-tag-{uuid_mod.uuid4().hex[:8]}" for _ in range(tag_count)]
            for name in names:
                db_session.add(
                    Tag(
                        uuid=str(uuid_mod.uuid4()),
                        name=name,
                        user_id=normal_user.id,
                        normalized_name=name,
                    )
                )
            db_session.flush()

            with count_selects(db_session) as counter:
                add_tags_to_file(db_session, media_file.id, names, normal_user.id)
            counts[tag_count] = counter["selects"]

            linked = (
                db_session.query(Tag.name)
                .join(FileTag, FileTag.tag_id == Tag.id)
                .filter(FileTag.media_file_id == media_file.id)
                .all()
            )
            assert {row[0] for row in linked} == set(names)

        assert counts[5] == counts[20], (
            f"SELECT count scales with tag count ({counts}) — the per-name loop is back "
            "(issue #284 A2.8)"
        )
        # Observed: one Tag lookup + one FileTag lookup + one read inside
        # redis_cache.invalidate_tags_for_file. Bounded, never per-name.
        assert counts[5] <= 4, f"expected a small constant number of SELECTs, got {counts[5]}"

    def test_missing_tags_are_created_and_owned_by_the_caller(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user)
        names = [f"new-tag-{uuid_mod.uuid4().hex[:8]}" for _ in range(3)]

        add_tags_to_file(db_session, media_file.id, names, normal_user.id)

        created = db_session.query(Tag).filter(Tag.name.in_(names)).all()
        assert len(created) == 3
        assert all(tag.user_id == normal_user.id for tag in created)

        linked = db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).count()
        assert linked == 3

    def test_an_owned_tag_beats_a_same_named_system_tag(self, db_session, normal_user):
        """ORDER BY user_id is ASC NULLS LAST, so the owned row wins."""
        media_file = _make_media_file(db_session, normal_user)
        name = f"shared-{uuid_mod.uuid4().hex[:8]}"
        system_tag = Tag(uuid=str(uuid_mod.uuid4()), name=name, user_id=None, normalized_name=name)
        owned_tag = Tag(
            uuid=str(uuid_mod.uuid4()), name=name, user_id=normal_user.id, normalized_name=name
        )
        db_session.add_all([system_tag, owned_tag])
        db_session.flush()

        add_tags_to_file(db_session, media_file.id, [name], normal_user.id)

        links = db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
        assert [link.tag_id for link in links] == [owned_tag.id]

    def test_a_system_tag_is_reused_when_no_owned_row_exists(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user)
        name = f"system-only-{uuid_mod.uuid4().hex[:8]}"
        system_tag = Tag(uuid=str(uuid_mod.uuid4()), name=name, user_id=None, normalized_name=name)
        db_session.add(system_tag)
        db_session.flush()

        add_tags_to_file(db_session, media_file.id, [name], normal_user.id)

        links = db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
        assert [link.tag_id for link in links] == [system_tag.id]

    def test_blank_and_duplicate_names_are_normalised(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user)
        name = f"dupe-{uuid_mod.uuid4().hex[:8]}"

        add_tags_to_file(
            db_session, media_file.id, [name, f"  {name}  ", "", "   "], normal_user.id
        )

        assert db_session.query(Tag).filter(Tag.name == name).count() == 1
        assert db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).count() == 1

    def test_long_names_are_truncated_to_50_chars(self, db_session, normal_user):
        media_file = _make_media_file(db_session, normal_user)
        long_name = "x" * 80

        add_tags_to_file(db_session, media_file.id, [long_name], normal_user.id)

        tag = db_session.query(Tag).filter(Tag.name == "x" * 50).first()
        assert tag is not None
        assert db_session.query(FileTag).filter(FileTag.tag_id == tag.id).count() == 1

    @pytest.mark.parametrize("tag_names", [[], ["", "  "]])
    def test_no_usable_names_is_a_no_op(self, db_session, normal_user, tag_names):
        media_file = _make_media_file(db_session, normal_user)
        add_tags_to_file(db_session, media_file.id, tag_names, normal_user.id)
        assert db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).count() == 0
