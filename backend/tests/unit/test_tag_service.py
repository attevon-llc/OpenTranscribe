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
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.services.auto_label_service import AutoLabelService
from app.services.tag_service import MAX_TAG_NAME_LENGTH
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import clean_tag_name
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import resolve_or_create_tag
from app.services.tag_service import suggest_similar_tag
from tests.conftest import engine


def _suffix() -> str:
    """Tag names are globally unique — every created name needs its own suffix."""
    return uuid.uuid4().hex[:8]


def _make_tag(db_session, name: str, *, source: str = "manual") -> Tag:
    tag = Tag(name=name, source=source, normalized_name=normalize_tag_name(name))
    db_session.add(tag)
    db_session.flush()
    return tag


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
    return db_session.query(Tag).filter(Tag.normalized_name == normalized).count()


def _commit_tag_on_other_connection(name: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO tag (name, source, normalized_name) VALUES (:n, 'manual', :norm)"),
            {"n": name, "norm": normalize_tag_name(name)},
        )
        conn.commit()


def _delete_tag_on_other_connection(name: str) -> None:
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM tag WHERE name = :n"), {"n": name})
        conn.commit()


@contextlib.contextmanager
def _competing_writer(db_session, contested: str):
    """Commit ``contested`` from another connection just before we insert it.

    Reproduces a genuinely lost race: the pre-check SELECT misses, another
    worker commits the row, and our INSERT hits the unique constraint.
    """
    fired = {"done": False}

    @event.listens_for(db_session, "before_flush")
    def _race(session, flush_context, instances):  # noqa: ANN001 - SQLAlchemy signature
        if fired["done"]:
            return
        if any(isinstance(obj, Tag) and obj.name == contested for obj in session.new):
            fired["done"] = True
            _commit_tag_on_other_connection(contested)

    try:
        yield
    finally:
        event.remove(db_session, "before_flush", _race)
        db_session.rollback()
        _delete_tag_on_other_connection(contested)


def test_case_only_difference_returns_existing_tag(db_session):
    """A name matching only by case resolves to the existing tag, creating no row."""
    name = f"Interview-{_suffix()}"
    existing = _make_tag(db_session, name)
    normalized = normalize_tag_name(name)

    resolved = resolve_or_create_tag(db_session, name.upper())

    assert resolved.id == existing.id
    assert _count_tags(db_session, normalized) == 1


@pytest.mark.parametrize("variant", ["{base} notes", "{base}_notes", "{base}-notes"])
def test_separator_and_whitespace_variants_return_existing_tag(db_session, variant):
    """Hyphen / underscore / repeated-whitespace variants collapse onto one tag."""
    base = f"quarterly{_suffix()}"
    canonical = f"{base}-notes"
    existing = _make_tag(db_session, canonical)

    resolved = resolve_or_create_tag(db_session, variant.format(base=base))

    assert resolved.id == existing.id
    assert _count_tags(db_session, normalize_tag_name(canonical)) == 1


def test_repeated_whitespace_returns_existing_tag(db_session):
    base = f"team{_suffix()}"
    existing = _make_tag(db_session, f"{base} sync")

    resolved = resolve_or_create_tag(db_session, f"  {base}   sync  ")

    assert resolved.id == existing.id


def test_unmatched_name_creates_one_tag_with_normalization_stored(db_session):
    """A name with no match creates exactly one tag carrying its normalized form."""
    name = f"Board_Meeting-{_suffix()}"
    normalized = normalize_tag_name(name)
    assert _count_tags(db_session, normalized) == 0

    created = resolve_or_create_tag(db_session, name)

    assert created.id is not None
    assert created.name == name
    assert created.normalized_name == normalized
    assert _count_tags(db_session, normalized) == 1


def test_near_match_creates_new_tag_when_fuzzy_not_requested(db_session):
    """A near match is NOT silently resolved — resolution is normalized-exact only."""
    suffix = _suffix()
    q3 = _make_tag(db_session, f"q3-earnings-{suffix}")
    q4_name = f"q4-earnings-{suffix}"

    # Sanity: these two are similar enough that a fuzzy resolver would merge them.
    assert suggest_similar_tag(db_session, q4_name) is not None

    resolved = resolve_or_create_tag(db_session, q4_name)

    assert resolved.id != q3.id
    assert resolved.name == q4_name


def test_suggestion_lookup_returns_near_match_when_asked(db_session):
    """The fuzzy scan is available as an explicit, opt-in suggestion lookup."""
    suffix = _suffix()
    q3 = _make_tag(db_session, f"q3-earnings-{suffix}")

    suggestion = suggest_similar_tag(db_session, f"q4-earnings-{suffix}")

    assert suggestion is not None
    assert suggestion.id == q3.id


def test_suggestion_lookup_returns_none_for_unrelated_name(db_session):
    _make_tag(db_session, f"q3-earnings-{_suffix()}")

    assert suggest_similar_tag(db_session, f"zzz-unrelated-topic-{_suffix()}") is None


def test_concurrent_insert_returns_winning_row_without_raising(db_session):
    """Losing the insert race returns the winner instead of propagating the error."""
    contested = f"race-{_suffix()}"

    with _competing_writer(db_session, contested):
        resolved = resolve_or_create_tag(db_session, contested)

        assert resolved.name == contested
        assert resolved.id is not None
        assert _count_tags(db_session, normalize_tag_name(contested)) == 1


def test_collision_leaves_callers_pending_writes_intact(db_session):
    """A lost race rolls back only the SAVEPOINT, never the caller's transaction."""
    contested = f"race-{_suffix()}"
    pending_name = f"pending-{_suffix()}"
    _make_tag(db_session, pending_name)

    with _competing_writer(db_session, contested):
        resolve_or_create_tag(db_session, contested)

        survivor = db_session.query(Tag).filter(Tag.name == pending_name).first()
        assert survivor is not None, "the caller's pending write was discarded"


def test_long_name_truncated_identically_across_paths(db_session, normal_user):
    """The 50-char clamp is the same whether the API or the upload path supplies it."""
    long_name = f"retro-{_suffix()}-" + ("x" * 80)
    assert len(long_name) > MAX_TAG_NAME_LENGTH

    resolved = resolve_or_create_tag(db_session, long_name)

    assert len(resolved.name) == MAX_TAG_NAME_LENGTH
    assert resolved.name == clean_tag_name(long_name)

    # The upload path supplies the same over-long name and must land on that row.
    media_file = _make_file(db_session, normal_user)
    add_tags_to_file(db_session, media_file.id, [long_name])

    links = (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == media_file.id, FileTag.tag_id == resolved.id)
        .all()
    )
    assert len(links) == 1
    assert _count_tags(db_session, normalize_tag_name(clean_tag_name(long_name))) == 1


@pytest.mark.parametrize("blank", ["", "   ", "-", "__", " - _ "])
def test_empty_after_normalization_is_rejected(db_session, blank):
    """A name that normalizes to nothing is rejected rather than stored blank."""
    before = db_session.query(Tag).count()

    with pytest.raises(InvalidTagNameError):
        resolve_or_create_tag(db_session, blank)

    assert db_session.query(Tag).count() == before


def test_upload_path_skips_blank_names(db_session, normal_user):
    """The upload path drops unusable names instead of failing the whole upload."""
    media_file = _make_file(db_session, normal_user)
    good_name = f"usable-{_suffix()}"

    add_tags_to_file(db_session, media_file.id, ["   ", good_name, "-"])

    links = db_session.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
    assert len(links) == 1
    assert links[0].tag.name == good_name


def test_auto_labeling_still_resolves_through_fuzzy_path(db_session):
    """Auto-labeling keeps its fuzzy dedup after delegating creation to the service."""
    suffix = _suffix()
    existing = _make_tag(db_session, f"q3 earnings review {suffix}", source="manual")

    service = AutoLabelService(db_session)
    resolved = service._get_or_create_tag_with_dedup(f"q3 earnings reviews {suffix}")

    assert resolved.id == existing.id


def test_auto_labeling_creates_through_shared_service(db_session):
    """A genuinely new auto-label name is created with normalization stored."""
    name = f"AI_Topic-{_suffix()}"
    service = AutoLabelService(db_session)

    created = service._get_or_create_tag_with_dedup(name)

    assert created.normalized_name == normalize_tag_name(name)
    assert _count_tags(db_session, normalize_tag_name(name)) == 1


def test_legacy_row_without_normalized_name_is_repaired_not_duplicated(db_session):
    """Rows seeded before this service existed carry no normalized_name.

    They must still resolve (via the exact-name fallback) and get backfilled, or
    every case variant of a seeded tag would fork into a second row.
    """
    name = f"Legacy-{_suffix()}"
    legacy = Tag(name=name, source="manual", normalized_name=None)
    db_session.add(legacy)
    db_session.flush()

    resolved = resolve_or_create_tag(db_session, name)

    assert resolved.id == legacy.id
    assert resolved.normalized_name == normalize_tag_name(name)

    # Now that it is backfilled, a case variant resolves onto the same row.
    assert resolve_or_create_tag(db_session, name.upper()).id == legacy.id
    assert _count_tags(db_session, normalize_tag_name(name)) == 1


def test_normalize_name_alias_delegates(db_session):
    """``AutoLabelService.normalize_name`` stays available as a delegating alias."""
    assert AutoLabelService.normalize_name("Foo_Bar  Baz") == normalize_tag_name("Foo_Bar  Baz")
    assert AutoLabelService.normalize_name("Foo_Bar  Baz") == "foo bar baz"
