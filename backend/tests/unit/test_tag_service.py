"""Behavioral tests for the shared tag resolution service.

Covers ``app/services/tag_service.py``: normalized-exact resolution, the
deliberate separation of the fuzzy suggestion lookup from resolution, the
50-character clamp shared with the upload path, and the SAVEPOINT collision
handling that must not discard the caller's transaction.

The race tests use a **real** competing writer: a second connection inserts and
commits the contested name at the moment the session under test tries to insert
it, so the ``IntegrityError`` comes from Postgres rather than a stub. That row is
committed outside the savepoint-isolated ``db_session``, so each of those tests
deletes it explicitly in a ``finally``.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy import text

from app.api.endpoints.files.prepare_upload import add_tags_to_file
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.topic import TopicSuggestion
from app.services.auto_label_service import AutoLabelService
from app.services.tag_service import MAX_TAG_NAME_LENGTH
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import clean_tag_name
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import on_tags_changed
from app.services.tag_service import resolve_or_create_tag
from app.services.tag_service import resolve_or_create_tags
from app.services.tag_service import suggest_similar_tag
from app.tasks.search_indexing_task import extract_file_index_metadata
from tests.conftest import engine


def _suffix() -> str:
    """Names are unique per owner — every created name still needs its own suffix."""
    return uuid.uuid4().hex[:8]


def _make_tag(db_session, name: str, *, source: str = "manual", user_id=None) -> Tag:
    """Create a tag. ``user_id=None`` makes a **system** tag, so pass an owner
    unless the test is specifically about the shared vocabulary."""
    tag = Tag(name=name, user_id=user_id, source=source, normalized_name=normalize_tag_name(name))
    db_session.add(tag)
    db_session.flush()
    return tag


def _make_collection(db_session, name: str, owner, source: str = "manual") -> Collection:
    collection = Collection(name=name, user_id=owner.id, source=source)
    db_session.add(collection)
    db_session.flush()
    return collection


def _make_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="tag_service_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _count_tags(db_session, normalized: str) -> int:
    return int(db_session.query(Tag).filter(Tag.normalized_name == normalized).count())


def _commit_tag_on_other_connection(name: str, user_id: int) -> None:
    """Commit the contested name as ``user_id`` — the owner matters.

    ``uq_tag_user_name`` is UNIQUE(user_id, name) WHERE user_id IS NOT NULL and
    ``uq_tag_name_system`` is UNIQUE(name) WHERE user_id IS NULL, so a row
    committed *ownerless* does not collide with an owned insert at all. Racing
    from a different owner would produce two legitimate rows and prove nothing.
    """
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO tag (name, user_id, source, normalized_name) "
                "VALUES (:n, :uid, 'manual', :norm)"
            ),
            {"n": name, "uid": user_id, "norm": normalize_tag_name(name)},
        )
        conn.commit()


def _delete_tag_on_other_connection(name: str) -> None:
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM tag WHERE name = :n"), {"n": name})
        conn.commit()


def _commit_user_on_other_connection(email: str) -> int:
    """Create a committed user the racing connection can attribute a tag to.

    ``db_session`` runs inside a savepoint that is never committed, so the
    fixture users it creates do not exist for any other connection —
    ``tag.user_id`` would fail its foreign key. Since ``v374_add_tag_user_id``
    the racer needs a *real* owner: an ownerless row lands in a different
    partial unique index (``uq_tag_name_system``) and would not collide at all,
    so the race it is supposed to reproduce would silently stop happening.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, full_name, is_active, role) '
                "VALUES (:e, 'x', 'Race Writer', true, 'user') RETURNING id"
            ),
            {"e": email},
        ).one()
        conn.commit()
        return int(row[0])


def _delete_user_on_other_connection(user_id: int) -> None:
    with engine.connect() as conn:
        conn.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": user_id})
        conn.commit()


@contextlib.contextmanager
def _competing_writer(db_session, contested: str):
    """Commit ``contested`` from another connection just before we insert it.

    Reproduces a genuinely lost race: the pre-check SELECT misses, another
    worker commits the row, and our INSERT hits the unique constraint. Yields
    the owner id to resolve as — racing a *different* owner produces two
    legitimate rows and proves nothing.
    """
    fired = {"done": False}
    racer_id = _commit_user_on_other_connection(f"race-{uuid.uuid4().hex[:10]}@example.com")

    @event.listens_for(db_session, "before_flush")
    def _race(session, flush_context, instances):  # noqa: ANN001 - SQLAlchemy signature
        if fired["done"]:
            return
        if any(isinstance(obj, Tag) and obj.name == contested for obj in session.new):
            fired["done"] = True
            _commit_tag_on_other_connection(contested, racer_id)

    try:
        yield racer_id
    finally:
        event.remove(db_session, "before_flush", _race)
        db_session.rollback()
        _delete_tag_on_other_connection(contested)
        _delete_user_on_other_connection(racer_id)


def test_case_only_difference_returns_existing_tag(db_session, normal_user):
    """A name matching only by case resolves to the existing tag, creating no row."""
    name = f"Interview-{_suffix()}"
    existing = _make_tag(db_session, name)
    normalized = normalize_tag_name(name)

    resolved = resolve_or_create_tag(db_session, name.upper(), user_id=normal_user.id)

    assert resolved.id == existing.id
    assert _count_tags(db_session, normalized) == 1


@pytest.mark.parametrize("variant", ["{base} notes", "{base}_notes", "{base}-notes"])
def test_separator_and_whitespace_variants_return_existing_tag(db_session, variant, normal_user):
    """Hyphen / underscore / repeated-whitespace variants collapse onto one tag."""
    base = f"quarterly{_suffix()}"
    canonical = f"{base}-notes"
    existing = _make_tag(db_session, canonical)

    resolved = resolve_or_create_tag(db_session, variant.format(base=base), user_id=normal_user.id)

    assert resolved.id == existing.id
    assert _count_tags(db_session, normalize_tag_name(canonical)) == 1


def test_repeated_whitespace_returns_existing_tag(db_session, normal_user):
    base = f"team{_suffix()}"
    existing = _make_tag(db_session, f"{base} sync")

    resolved = resolve_or_create_tag(db_session, f"  {base}   sync  ", user_id=normal_user.id)

    assert resolved.id == existing.id


def test_unmatched_name_creates_one_tag_with_normalization_stored(db_session, normal_user):
    """A name with no match creates exactly one tag carrying its normalized form."""
    name = f"Board_Meeting-{_suffix()}"
    normalized = normalize_tag_name(name)
    assert _count_tags(db_session, normalized) == 0

    created = resolve_or_create_tag(db_session, name, user_id=normal_user.id)

    assert created.id is not None
    assert created.name == name
    assert created.normalized_name == normalized
    assert _count_tags(db_session, normalized) == 1


def test_near_match_creates_new_tag_when_fuzzy_not_requested(db_session, normal_user):
    """A near match is NOT silently resolved — resolution is normalized-exact only."""
    suffix = _suffix()
    q3 = _make_tag(db_session, f"q3-earnings-{suffix}")
    q4_name = f"q4-earnings-{suffix}"

    # Sanity: these two are similar enough that a fuzzy resolver would merge them.
    assert suggest_similar_tag(db_session, q4_name, user_id=normal_user.id) is not None

    resolved = resolve_or_create_tag(db_session, q4_name, user_id=normal_user.id)

    assert resolved.id != q3.id
    assert resolved.name == q4_name


def test_suggestion_lookup_returns_near_match_when_asked(db_session, normal_user):
    """The fuzzy scan is available as an explicit, opt-in suggestion lookup."""
    suffix = _suffix()
    q3 = _make_tag(db_session, f"q3-earnings-{suffix}")

    suggestion = suggest_similar_tag(db_session, f"q4-earnings-{suffix}", user_id=normal_user.id)

    assert suggestion is not None
    assert suggestion.id == q3.id


def test_suggestion_lookup_returns_none_for_unrelated_name(db_session, normal_user):
    _make_tag(db_session, f"q3-earnings-{_suffix()}")

    assert (
        suggest_similar_tag(db_session, f"zzz-unrelated-topic-{_suffix()}", user_id=normal_user.id)
        is None
    )


def test_concurrent_insert_returns_winning_row_without_raising(db_session, normal_user):
    """Losing the insert race returns the winner instead of propagating the error."""
    contested = f"race-{_suffix()}"

    with _competing_writer(db_session, contested) as racer_id:
        resolved = resolve_or_create_tag(db_session, contested, user_id=racer_id)

        assert resolved.name == contested
        assert resolved.id is not None
        assert _count_tags(db_session, normalize_tag_name(contested)) == 1


def test_collision_leaves_callers_pending_writes_intact(db_session, normal_user):
    """A lost race rolls back only the SAVEPOINT, never the caller's transaction."""
    contested = f"race-{_suffix()}"
    pending_name = f"pending-{_suffix()}"
    _make_tag(db_session, pending_name, user_id=normal_user.id)

    with _competing_writer(db_session, contested) as racer_id:
        resolved = resolve_or_create_tag(db_session, contested, user_id=racer_id)

        # The collision has to have actually happened, or "the pending write survived"
        # is true for the boring reason that nothing was ever rolled back.
        assert _count_tags(db_session, normalize_tag_name(contested)) == 1
        assert resolved.name == contested

        survivor = db_session.query(Tag).filter(Tag.name == pending_name).first()
        assert survivor is not None, "the caller's pending write was discarded"
        assert survivor.user_id == normal_user.id
        assert survivor.name == pending_name


def test_long_name_truncated_identically_across_paths(db_session, normal_user):
    """The 50-char clamp is the same whether the API or the upload path supplies it."""
    long_name = f"retro-{_suffix()}-" + ("x" * 80)
    assert len(long_name) > MAX_TAG_NAME_LENGTH

    resolved = resolve_or_create_tag(db_session, long_name, user_id=normal_user.id)

    assert len(resolved.name) == MAX_TAG_NAME_LENGTH
    assert resolved.name == clean_tag_name(long_name)

    # The upload path supplies the same over-long name and must land on that row.
    media_file = _make_file(db_session, normal_user)
    add_tags_to_file(db_session, media_file.id, [long_name], normal_user.id)

    links = (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == media_file.id, FileTag.tag_id == resolved.id)
        .all()
    )
    assert len(links) == 1
    assert _count_tags(db_session, normalize_tag_name(clean_tag_name(long_name))) == 1


@pytest.mark.parametrize("blank", ["", "   ", "-", "__", " - _ "])
def test_empty_after_normalization_is_rejected(db_session, blank, normal_user):
    """A name that normalizes to nothing is rejected rather than stored blank.

    Asserted against this session's pending writes and the blank-name rows
    specifically, never a global ``COUNT(*)`` — the race tests in this module
    commit tags on a second connection, so under ``-n auto`` a global count
    moves underneath an unrelated test.
    """
    with pytest.raises(InvalidTagNameError):
        resolve_or_create_tag(db_session, blank, user_id=normal_user.id)

    assert [obj for obj in db_session.new if isinstance(obj, Tag)] == []
    assert db_session.query(Tag).filter(Tag.normalized_name == "").count() == 0
    assert db_session.query(Tag).filter(Tag.name == blank.strip()).count() == 0


def test_upload_path_skips_blank_names(db_session, normal_user):
    """The upload path drops unusable names instead of failing the whole upload."""
    media_file = _make_file(db_session, normal_user)
    good_name = f"usable-{_suffix()}"

    add_tags_to_file(db_session, media_file.id, ["   ", good_name, "-"], normal_user.id)

    links = db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
    assert len(links) == 1
    assert links[0].tag.name == good_name


def test_auto_labeling_still_resolves_through_fuzzy_path(db_session, normal_user):
    """Auto-labeling keeps its fuzzy dedup after delegating creation to the service."""
    suffix = _suffix()
    existing = _make_tag(db_session, f"q3 earnings review {suffix}", source="manual")

    service = AutoLabelService(db_session)
    resolved = service._get_or_create_tag_with_dedup(
        f"q3 earnings reviews {suffix}", normal_user.id
    )

    assert resolved.id == existing.id


def test_auto_labeling_creates_through_shared_service(db_session, normal_user):
    """A genuinely new auto-label name is created with normalization stored."""
    name = f"AI_Topic-{_suffix()}"
    service = AutoLabelService(db_session)

    created = service._get_or_create_tag_with_dedup(name, normal_user.id)

    assert created.normalized_name == normalize_tag_name(name)
    assert _count_tags(db_session, normalize_tag_name(name)) == 1


def test_legacy_row_without_normalized_name_is_repaired_not_duplicated(db_session, normal_user):
    """Rows seeded before this service existed carry no normalized_name.

    They must still resolve (via the exact-name fallback) and get backfilled, or
    every case variant of a seeded tag would fork into a second row.
    """
    name = f"Legacy-{_suffix()}"
    legacy = Tag(name=name, source="manual", normalized_name=None)
    db_session.add(legacy)
    db_session.flush()

    resolved = resolve_or_create_tag(db_session, name, user_id=normal_user.id)

    assert resolved.id == legacy.id
    assert resolved.normalized_name == normalize_tag_name(name)

    # Now that it is backfilled, a case variant resolves onto the same row.
    assert resolve_or_create_tag(db_session, name.upper(), user_id=normal_user.id).id == legacy.id
    assert _count_tags(db_session, normalize_tag_name(name)) == 1


def test_normalize_name_alias_delegates(db_session):
    """``AutoLabelService.normalize_name`` stays available as a delegating alias."""
    assert AutoLabelService.normalize_name("Foo_Bar  Baz") == normalize_tag_name("Foo_Bar  Baz")
    assert AutoLabelService.normalize_name("Foo_Bar  Baz") == "foo bar baz"


# ---------------------------------------------------------------------------
# Search-index metadata extraction
# ---------------------------------------------------------------------------


def test_indexed_document_carries_tags_applied_before_transcription(db_session, normal_user):
    """Tags on a file reach the search document.

    ``MediaFile`` declares ``file_tags``, never ``tags``; extracting through the
    latter left every indexed transcript with an empty tags array, so
    search-by-tag silently matched nothing.
    """
    media_file = _make_file(db_session, normal_user)
    tag = _make_tag(db_session, f"pre-transcription-{_suffix()}")
    media_file.file_tags.append(FileTag(tag_id=tag.id, source="manual"))
    db_session.flush()

    meta = extract_file_index_metadata(db_session, media_file, media_file.id)

    assert meta["tag_names"] == [tag.name]


def test_indexed_document_carries_collection_ids(db_session, normal_user):
    """Collection membership reaches the search document (same class of bug)."""
    media_file = _make_file(db_session, normal_user)
    collection = Collection(name=f"coll-{_suffix()}", user_id=normal_user.id)
    db_session.add(collection)
    db_session.flush()
    media_file.collection_memberships.append(CollectionMember(collection_id=collection.id))
    db_session.flush()

    meta = extract_file_index_metadata(db_session, media_file, media_file.id)

    assert meta["collection_ids"] == [collection.id]


# ---------------------------------------------------------------------------
# The shared cache + search-refresh hook
# ---------------------------------------------------------------------------


class _RecordingTask:
    """Stands in for the Celery task so the enqueued payload is observable."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def delay(self, file_ids):  # noqa: ANN001,ANN202 - mirrors Task.delay
        self.calls.append(list(file_ids))


@pytest.fixture
def recorded_reindex(monkeypatch):
    """Capture what ``on_tags_changed`` hands to the partial-update task."""
    from app.tasks import search_indexing_task

    recorder = _RecordingTask()
    monkeypatch.setattr(search_indexing_task, "update_file_tags_index", recorder)
    return recorder


def test_attaching_tag_enqueues_reindex_for_that_file(
    client, db_session, normal_user, user_token_headers, recorded_reindex
):
    """The attach endpoint refreshes the file's search document."""
    media_file = _make_file(db_session, normal_user)

    response = client.post(
        f"/api/tags/files/{media_file.uuid}/tags",
        json={"name": f"attach-{_suffix()}"},
        headers=user_token_headers,
    )

    assert response.status_code == 200, response.text
    assert recorded_reindex.calls == [[media_file.id]]


def test_detaching_tag_enqueues_reindex(
    client, db_session, normal_user, user_token_headers, recorded_reindex
):
    """Detach is a tag change too — without a refresh the index keeps the name."""
    media_file = _make_file(db_session, normal_user)
    tag = _make_tag(db_session, f"detach-{_suffix()}")
    media_file.file_tags.append(FileTag(tag_id=tag.id, source="manual"))
    db_session.flush()

    response = client.delete(
        f"/api/tags/files/{media_file.uuid}/tags/{tag.name}",
        headers=user_token_headers,
    )

    assert response.status_code == 204, response.text
    assert recorded_reindex.calls == [[media_file.id]]


def test_auto_labeled_tags_enqueue_one_reindex_for_the_file(
    db_session, normal_user, recorded_reindex
):
    """The auto-labeler goes through the same hook as the human-driven paths.

    Once for the whole batch, not once per applied tag: the hook drops **every**
    user's cached tag list (a keyspace-wide operation) and enqueues a refresh
    that rewrites the same document, so two tags on one file is one file's worth
    of work, not two.
    """
    media_file = _make_file(db_session, normal_user)
    suggestion = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=normal_user.id,
        suggested_tags=[
            {"name": f"auto-alpha-{_suffix()}", "confidence": 0.95},
            {"name": f"zeta-bravo-{_suffix()}", "confidence": 0.95},
        ],
        suggested_collections=[],
    )
    db_session.add(suggestion)
    db_session.flush()

    result = AutoLabelService(db_session).auto_apply_suggestions(
        media_file=media_file,
        suggestion=suggestion,
        user_id=normal_user.id,
    )

    assert len(result["auto_applied_tags"]) == 2
    assert recorded_reindex.calls == [[media_file.id]]


def test_upload_path_enqueues_reindex(db_session, normal_user, recorded_reindex):
    """Tags supplied at upload reach the index without waiting for a full reindex."""
    media_file = _make_file(db_session, normal_user)

    add_tags_to_file(db_session, media_file.id, [f"uploaded-{_suffix()}"], normal_user.id)

    assert recorded_reindex.calls == [[media_file.id]]


def test_multi_file_mutation_enqueues_each_file_once(db_session, normal_user, recorded_reindex):
    """A merge touching several files refreshes each exactly once, in one task.

    The refresh is one task carrying the whole list — not the per-user reindex
    coordinator, which self-skips under its lock on exactly this shape of merge.
    """
    first = _make_file(db_session, normal_user)
    second = _make_file(db_session, normal_user)

    affected = on_tags_changed(
        db_session,
        [first.id, second.id, first.id, second.id],
        user_id=normal_user.id,
    )

    assert affected == [first.id, second.id]
    assert recorded_reindex.calls == [[first.id, second.id]]


@pytest.mark.xdist_group("tag_cache_global")
def test_global_invalidation_clears_a_bystanders_cached_tag_list(
    db_session, normal_user, other_user, recorded_reindex
):
    """Tags are global rows behind a per-user cache key.

    Busting only the actor's key leaves every other user reading the old list
    for the rest of its TTL. Needs the dev stack's Redis.
    """
    from app.services.redis_cache_service import redis_cache

    if redis_cache.redis is None:
        pytest.skip("Redis unreachable — this scenario asserts real cache eviction")

    actor_key = f"cache:tags:{normal_user.id}"
    bystander_key = f"cache:tags:{other_user.id}"
    redis_cache.set(actor_key, [{"name": "stale"}], ttl=60)
    redis_cache.set(bystander_key, [{"name": "stale"}], ttl=60)
    assert redis_cache.get(bystander_key) is not None

    media_file = _make_file(db_session, normal_user)
    on_tags_changed(db_session, [media_file.id], user_id=normal_user.id)

    assert redis_cache.get(actor_key) is None
    assert redis_cache.get(bystander_key) is None, "another user's tag list stayed stale"


# ---------------------------------------------------------------------------
# Ownership scoping (v374_add_tag_user_id)
#
# The branch these tests arrived on was written when `tag` had no owner column.
# Every assertion here fails against an unscoped resolver, which is the point:
# resolving without `owned_or_system` attaches a typed name to whichever
# account's row the planner returned first, and creating without an owner
# publishes the tag to every account.
# ---------------------------------------------------------------------------


def test_created_tag_is_owned_by_the_acting_user(db_session, normal_user):
    """A resolver-created tag is never ownerless — that would be a system tag."""
    name = f"owned-{_suffix()}"

    tag = resolve_or_create_tag(db_session, name, user_id=normal_user.id)

    assert tag.user_id == normal_user.id, "an ownerless tag is published to every account"


def test_resolution_does_not_cross_to_another_users_tag(db_session, normal_user, other_user):
    """Typing a name another account already uses creates YOUR row, not theirs."""
    name = f"Interview-{_suffix()}"
    theirs = _make_tag(db_session, name, user_id=other_user.id)

    mine = resolve_or_create_tag(db_session, name, user_id=normal_user.id)

    assert mine.id != theirs.id
    assert mine.user_id == normal_user.id


def test_own_tag_beats_a_same_named_system_tag(db_session, normal_user):
    """An owned row wins over the shared row (ORDER BY user_id is NULLS LAST)."""
    name = f"Meeting-{_suffix()}"
    _make_tag(db_session, name, user_id=None)
    owned = _make_tag(db_session, name, user_id=normal_user.id)

    assert resolve_or_create_tag(db_session, name, user_id=normal_user.id).id == owned.id


def test_system_tag_is_reused_rather_than_forked(db_session, normal_user):
    """Applying a seeded default attaches the shared row, not a private copy."""
    name = f"Important-{_suffix()}"
    system = _make_tag(db_session, name, user_id=None)

    resolved = resolve_or_create_tag(db_session, name, user_id=normal_user.id)

    assert resolved.id == system.id
    assert resolved.user_id is None


def test_suggestions_never_cross_accounts(db_session, normal_user, other_user):
    """The fuzzy scan must not surface — or auto-apply — another account's tag.

    The auto-labeler applies its fuzzy hit without confirmation, so an unscoped
    pool would silently attach a row the acting user does not own.
    """
    suffix = _suffix()
    _make_tag(db_session, f"q3-earnings-{suffix}", user_id=other_user.id)

    assert suggest_similar_tag(db_session, f"q4-earnings-{suffix}", user_id=normal_user.id) is None


def test_two_users_tagging_one_shared_file_reuse_the_same_row(db_session, normal_user, other_user):
    """The deconfliction case: one file must never carry the same word twice.

    Without ``lookup_tag_on_file`` the second user forks their own row, the file
    renders "interview" twice, and the gallery's ALL-filter has to count
    DISTINCT names to compensate.
    """
    media_file = _make_file(db_session, normal_user)
    name = f"interview-{_suffix()}"

    first = resolve_or_create_tag(db_session, name, user_id=normal_user.id, file_id=media_file.id)
    db_session.add(FileTag(media_file_id=media_file.id, tag_id=first.id, source="manual"))
    db_session.flush()

    second = resolve_or_create_tag(
        db_session, name.upper(), user_id=other_user.id, file_id=media_file.id
    )

    assert second.id == first.id, "second tagger forked a duplicate row onto the same file"


def test_batch_resolver_matches_the_single_resolver(db_session, normal_user):
    """``resolve_or_create_tags`` is the batched path — same semantics, one query.

    Upload prepare uses it, so a divergence here would mean a name typed at
    upload resolves differently from the same name typed on the detail page.
    """
    base = f"quarterly{_suffix()}"
    single = resolve_or_create_tag(db_session, f"{base}-notes", user_id=normal_user.id)

    batched = resolve_or_create_tags(
        db_session, [f"{base}_NOTES", f"{base} notes", "", "   "], user_id=normal_user.id
    )

    assert [tag.id for tag in batched] == [single.id], "variants must collapse onto one tag"


def test_batch_resolver_owns_what_it_creates(db_session, normal_user):
    """The bulk path must not create ownerless rows either."""
    created = resolve_or_create_tags(
        db_session, [f"alpha-{_suffix()}", f"beta-{_suffix()}"], user_id=normal_user.id
    )

    assert len(created) == 2
    assert all(tag.user_id == normal_user.id for tag in created)


# ---------------------------------------------------------------------------
# Auto-label tenancy scoping (issue #587)
#
# AutoLabelService is already correctly scoped (per-instance, per-user-id-keyed
# caches; _owned_or_system unions the acting user's rows with the system tier).
# These are regression guards for PR #488's leak shape: tag list endpoints once
# had no user filter, so one user's unattached tags leaked into another's view.
# ---------------------------------------------------------------------------


def test_auto_label_tag_lookup_does_not_cross_to_another_users_same_named_tag(
    db_session, normal_user, other_user
):
    """A same-named tag owned by another account must not resolve as a match."""
    name = f"Budget {_suffix()}"
    _make_tag(db_session, name, user_id=other_user.id)

    assert AutoLabelService(db_session).find_existing_similar_tag(name, normal_user.id) is None


def test_auto_label_creates_its_own_row_rather_than_reusing_another_users(
    db_session, normal_user, other_user
):
    """Failing to find a match, the service must create A's own row, not reuse B's."""
    name = f"Budget {_suffix()}"
    other_tag = _make_tag(db_session, name, user_id=other_user.id)

    svc = AutoLabelService(db_session)
    tag = svc._get_or_create_tag_with_dedup(name, normal_user.id)

    assert tag.user_id == normal_user.id
    assert tag.id != other_tag.id


def test_auto_label_tag_cache_is_not_shared_between_users(db_session, normal_user, other_user):
    """The per-instance tag cache must be keyed per user, never a flat shared list."""
    name = f"Budget {_suffix()}"
    other_tag = _make_tag(db_session, name, user_id=other_user.id)

    svc = AutoLabelService(db_session)
    b_tags = svc._get_all_tags_cached(other_user.id)
    a_tags = svc._get_all_tags_cached(normal_user.id)

    assert other_tag.id in {t.id for t in b_tags}
    assert other_tag.id not in {t.id for t in a_tags}
    assert set(svc._tag_cache.keys()) == {other_user.id, normal_user.id}


def test_auto_label_system_tags_are_visible_to_every_user(db_session, normal_user, other_user):
    """A system (ownerless) tag is shared vocabulary — every user sees it."""
    name = f"System-{_suffix()}"
    system_tag = _make_tag(db_session, name, user_id=None)

    svc = AutoLabelService(db_session)
    a_tags = svc._get_all_tags_cached(normal_user.id)
    b_tags = svc._get_all_tags_cached(other_user.id)

    assert system_tag.id in {t.id for t in a_tags}
    assert system_tag.id in {t.id for t in b_tags}

    found = svc.find_existing_similar_tag(name, normal_user.id)
    assert found is not None
    assert found.id == system_tag.id
    assert found.user_id is None


def test_own_tag_wins_over_a_same_named_system_tag_on_the_auto_label_path(db_session, normal_user):
    """Pins the ORDER BY user_id NULLS-LAST fast path in find_existing_similar_tag."""
    name = f"Meeting-{_suffix()}"
    _make_tag(db_session, name, user_id=None)
    owned = _make_tag(db_session, name, user_id=normal_user.id)

    found = AutoLabelService(db_session).find_existing_similar_tag(name, normal_user.id)

    assert found is not None
    assert found.id == owned.id


def test_auto_label_collection_lookup_does_not_cross_accounts(db_session, normal_user, other_user):
    """A collection owned by another account must not resolve, or be reused, as a match."""
    name = f"Quarterly Reviews {_suffix()}"
    other_collection = _make_collection(db_session, name, other_user)

    svc = AutoLabelService(db_session)
    assert svc.find_existing_similar_collection(normal_user.id, name) is None

    created = svc._get_or_create_collection_with_dedup(name, normal_user.id)
    assert created.user_id == normal_user.id
    assert created.id != other_collection.id


def test_auto_label_collection_cache_is_not_shared_between_users(
    db_session, normal_user, other_user
):
    """The per-instance collection cache must be keyed per user, never a flat shared list."""
    name = f"Quarterly Reviews {_suffix()}"
    other_collection = _make_collection(db_session, name, other_user)

    svc = AutoLabelService(db_session)
    b_collections = svc._get_user_collections_cached(other_user.id)
    a_collections = svc._get_user_collections_cached(normal_user.id)

    assert other_collection.id in {c.id for c in b_collections}
    assert other_collection.id not in {c.id for c in a_collections}
    assert set(svc._collection_cache.keys()) == {other_user.id, normal_user.id}


def test_fuzzy_match_does_not_reach_another_users_near_miss(db_session, normal_user, other_user):
    """The fuzzy leg bypasses the normalized-exact index — the likeliest regression point."""
    suffix = _suffix()
    other_tag = _make_tag(db_session, f"Quarterly Planning {suffix}", user_id=other_user.id)

    svc = AutoLabelService(db_session)
    created = svc._get_or_create_tag_with_dedup(f"Quarterly Plannning {suffix}", normal_user.id)

    assert created.user_id == normal_user.id
    assert created.id != other_tag.id


def test_auto_apply_never_applies_another_users_tag_row(db_session, normal_user, other_user):
    """End-to-end via auto_apply_suggestions: never attach another user's tag row."""
    name = f"Budget {_suffix()}"
    other_tag = _make_tag(db_session, name, user_id=other_user.id)
    media_file = _make_file(db_session, normal_user)

    suggestion = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=normal_user.id,
        suggested_tags=[{"name": name, "confidence": 0.95}],
        suggested_collections=[],
    )
    db_session.add(suggestion)
    db_session.flush()

    AutoLabelService(db_session).auto_apply_suggestions(
        media_file=media_file,
        suggestion=suggestion,
        user_id=normal_user.id,
    )

    file_tag = (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == media_file.id)
        .join(Tag, Tag.id == FileTag.tag_id)
        .filter(Tag.name == name)
        .first()
    )
    assert file_tag is not None
    assert file_tag.tag.user_id == normal_user.id
    assert file_tag.tag_id != other_tag.id
