"""v374 migration + detection-arm consistency (per-user tag ownership).

The alembic chain must contain v374 (revises v373), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v374-shape schema by its
relational marker (``tag.user_id``). The detection test runs against the live
test DB (which carries the applied chain), so it also proves the v374 DDL
actually produced the shape the detector keys on.

``test_backfill_splits_mixed_ownership_tags`` replays the revision's backfill
against freshly-seeded rows — the block is written to be re-runnable, so it
still fires on a post-migration schema and proves the split rule directly.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.user import User

REVISION = "v374_add_tag_user_id"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision file by path.

    ``alembic/`` is not an importable package — the installed alembic library
    shadows it (which is why ``_repair_skipped_v230`` carries a raw-SQL
    fallback), so ``from alembic.versions... import`` does not work here.
    """
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v374_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v373_add_cluster_organization_id"
    # Exactly one head, and v374 is it unless a later revision (v375+) has landed.
    heads = set(scripts.get_heads())
    assert len(heads) == 1
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v374_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    source = _REVISION_PATH.read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v374_on_current_schema(db_session):
    """An untracked DB carrying v374's markers must never stamp EARLIER than v374."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


def test_tag_user_id_column_and_indexes_exist(db_session):
    """The v374 DDL produced the column + partial unique indexes the code relies on."""
    conn = db_session.connection()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='tag' AND column_name='user_id')"
        )
    ).scalar()
    for index_name in ("uq_tag_user_name", "uq_tag_system_name", "ix_tag_user_id"):
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname=:n)"),
            {"n": index_name},
        ).scalar(), f"missing index {index_name}"


def test_global_unique_on_tag_name_is_gone(db_session):
    """UNIQUE(name) must be dropped or two users could not both own 'Meeting'."""
    conn = db_session.connection()
    remaining = conn.execute(
        text(
            "SELECT conname FROM pg_constraint WHERE conrelid='tag'::regclass "
            "AND contype='u' AND conkey = ARRAY[(SELECT attnum FROM pg_attribute "
            "WHERE attrelid='tag'::regclass AND attname='name')]"
        )
    ).fetchall()
    assert remaining == []


def _make_user(db_session) -> User:
    user = User(
        email=f"v374_{uuid_pkg.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _make_file(db_session, user: User) -> MediaFile:
    media_file = MediaFile(
        filename=f"v374_{uuid_pkg.uuid4().hex[:8]}.mp3",
        storage_path=f"v374/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/mpeg",
        user_id=user.id,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def test_backfill_splits_mixed_ownership_tags(db_session):
    """A tag on two users' files becomes one owned row per user, nothing shared.

    This is the mixed-ownership rule: the lowest-numbered owner keeps the
    original row (so external references stay valid) and every other owner gets
    their own copy with their ``file_tag`` rows repointed at it.
    """
    backfill_sql = _revision_module().BACKFILL_SQL

    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    low, high = sorted((user_a, user_b), key=lambda u: u.id)

    file_a = _make_file(db_session, user_a)
    file_b = _make_file(db_session, user_b)

    shared_name = f"v374 shared {uuid_pkg.uuid4().hex[:8]}"
    tag = Tag(name=shared_name, user_id=None, source="manual", normalized_name=shared_name)
    db_session.add(tag)
    db_session.flush()
    original_tag_id = tag.id

    db_session.add(FileTag(media_file_id=file_a.id, tag_id=tag.id, source="manual"))
    db_session.add(FileTag(media_file_id=file_b.id, tag_id=tag.id, source="manual"))
    db_session.flush()

    db_session.execute(text(backfill_sql))
    db_session.flush()
    db_session.expire_all()

    rows = db_session.query(Tag).filter(Tag.name == shared_name).all()
    assert {r.user_id for r in rows} == {low.id, high.id}
    assert len(rows) == 2

    # The lowest-numbered owner keeps the original row id.
    kept = next(r for r in rows if r.id == original_tag_id)
    assert kept.user_id == low.id

    # Every attachment now points at a tag its own file owner owns.
    mismatched = (
        db_session.query(FileTag)
        .join(Tag, Tag.id == FileTag.tag_id)
        .join(MediaFile, MediaFile.id == FileTag.media_file_id)
        .filter(Tag.name == shared_name, Tag.user_id != MediaFile.user_id)
        .count()
    )
    assert mismatched == 0


def test_backfill_leaves_unattached_tags_as_system_tags(db_session):
    """Tags attached to nobody stay NULL — this is what keeps the seeded defaults."""
    backfill_sql = _revision_module().BACKFILL_SQL

    orphan_name = f"v374 orphan {uuid_pkg.uuid4().hex[:8]}"
    db_session.add(Tag(name=orphan_name, user_id=None))
    db_session.flush()

    db_session.execute(text(backfill_sql))
    db_session.flush()
    db_session.expire_all()

    orphan = db_session.query(Tag).filter(Tag.name == orphan_name).one()
    assert orphan.user_id is None


def test_seeded_default_tags_survive_as_system_tags(db_session):
    """The four seeded defaults exist ownerless after the migration + seeder."""
    from app.initial_data import _ensure_default_tags

    _ensure_default_tags(db_session)

    system_names = {row.name for row in db_session.query(Tag).filter(Tag.user_id.is_(None)).all()}
    assert {"Important", "Meeting", "Interview", "Personal"} <= system_names


def test_two_users_can_own_the_same_tag_name(db_session):
    """The point of the schema change: 'Meeting' is no longer globally unique."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    name = f"v374 dup {uuid_pkg.uuid4().hex[:8]}"

    db_session.add(Tag(name=name, user_id=user_a.id))
    db_session.add(Tag(name=name, user_id=user_b.id))
    db_session.flush()

    assert db_session.query(Tag).filter(Tag.name == name).count() == 2


def test_one_user_cannot_own_the_same_tag_name_twice(db_session):
    """uq_tag_user_name still prevents per-user duplicates."""
    from sqlalchemy.exc import IntegrityError

    user = _make_user(db_session)
    name = f"v374 uniq {uuid_pkg.uuid4().hex[:8]}"

    db_session.add(Tag(name=name, user_id=user.id))
    db_session.flush()

    db_session.begin_nested()
    db_session.add(Tag(name=name, user_id=user.id))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_system_tag_names_stay_unique(db_session):
    """uq_tag_system_name keeps the seeder idempotent despite the NULL owner."""
    from sqlalchemy.exc import IntegrityError

    name = f"v374 sys {uuid_pkg.uuid4().hex[:8]}"
    db_session.add(Tag(name=name, user_id=None))
    db_session.flush()

    db_session.begin_nested()
    db_session.add(Tag(name=name, user_id=None))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
