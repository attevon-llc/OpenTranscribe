"""The ORM's ``delete-orphan`` declarations, and the four places the database disagrees.

What was missing
----------------
31 relationships in ``app/models`` declare ``cascade="all, delete-orphan"``. Three had a
test proving the children actually go (``ChatConversation.messages``,
``UserGroup.mappings``, ``SpeakerCluster.members``), and the entire non-e2e tree
contained exactly **three** ``db_session.delete()`` calls. A ``cascade=`` argument is
one keyword away from being dropped, and dropping it is silent: the parent deletes, the
children are left pointing at a row that no longer exists (or, for the four foreign keys
below, the delete simply fails).

The asymmetry this module exists to pin
---------------------------------------
``MediaFile`` declares ``delete-orphan`` on eight relationships. **Four of the eight
foreign keys are ``ON DELETE NO ACTION``** — ``transcript_segment``, ``comment``,
``task`` and ``analytics`` on ``media_file_id`` — and the other four are ``CASCADE``
(``file_tag``, ``collection_member``, ``speaker``, ``topic_suggestion``).

That split has a consequence nobody wrote down, and it splits the delete paths:

* An **instance** delete (``db.delete(file)``) fires the ORM cascade, so all eight go.
  That is what ``file_cleanup_service.purge_media_file`` does, which is why the GDPR
  erasure path works. (Its docstring says "CASCADE removes child rows"; for four of
  the eight the mechanism is the *ORM*, not the database.)
* A **bulk** delete (``query(MediaFile).delete()``) emits one statement, loads no
  instances, and fires no ORM cascade at all. The database is then the only enforcement
  left — and for those four it enforces refusal. That is
  ``admin._delete_user_media_files``, which hand-deletes segments, file_tags and
  analytics and, until this suite, **not comments or tasks**.

:func:`test_a_bulk_delete_is_refused_by_each_no_action_child` and
:func:`test_a_bulk_delete_sweeps_each_cascading_child` are the paired halves that pin
which four are which, so the split is a checked fact rather than something to rediscover.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from app.models.custom_vocabulary import CustomVocabulary
from app.models.file_facts import FileFacts
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Analytics
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import Comment
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.password_history import PasswordHistory
from app.models.refresh_token import RefreshToken
from app.models.topic import TopicSuggestion
from app.models.user_asr_settings import UserASRSettings
from app.models.user_diarization_settings import UserDiarizationSettings
from app.models.user_media_source import UserMediaSource
from app.models.user_mfa import UserMFA
from tests.user_owned_rows import make_media_file
from tests.user_owned_rows import make_user

#: ``MediaFile`` child -> the model, and whether the DATABASE also cascades.
#: ``False`` means the ORM declaration is the ONLY thing that removes the row, so a bulk
#: parent delete is refused rather than cascading.
#: ``Any`` for the model, not ``type[Base]``: the tests read ``.id`` off it, which the
#: declarative base does not declare.
_MEDIA_FILE_CHILDREN: dict[str, tuple[Any, bool]] = {
    "transcript_segments": (TranscriptSegment, False),
    "comments": (Comment, False),
    "tasks": (Task, False),
    "analytics": (Analytics, False),
    "file_tags": (FileTag, True),
    "collection_memberships": (CollectionMember, True),
    "speakers": (Speaker, True),
    "topic_suggestions": (TopicSuggestion, True),
    # v389: the FK is ON DELETE CASCADE, so the database removes the row too — the one
    # place in this schema where a derived row is deliberately cascaded rather than
    # NO ACTION'd (there is nothing to re-expose by deleting a summary of a deleted file).
    "facts_row": (FileFacts, True),
}

#: The four whose FK refuses a bulk parent delete, and the constraint that does the
#: refusing. Asserting the constraint NAME is what makes the test specific: any
#: ``IntegrityError`` would otherwise satisfy it, including one from an unrelated bug in
#: the fixture.
_NO_ACTION_CONSTRAINTS = {
    "transcript_segments": "transcript_segment_media_file_id_fkey",
    "comments": "comment_media_file_id_fkey",
    "tasks": "task_media_file_id_fkey",
    "analytics": "analytics_media_file_id_fkey",
}


def _seed_child(db, media_file: MediaFile, relationship_name: str) -> Any:
    """Create exactly ONE child of ``media_file`` on the named relationship.

    One at a time on purpose: with all eight present, a bulk parent delete raises on
    whichever foreign key Postgres happens to check first, so the test would pass while
    proving nothing about the other three.
    """
    row: Any
    user_id = int(media_file.user_id)
    if relationship_name == "transcript_segments":
        row = TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            start_time=0.0,
            end_time=1.0,
            text="child",
        )
    elif relationship_name == "comments":
        row = Comment(
            uuid=uuid_pkg.uuid4(), media_file_id=media_file.id, user_id=user_id, text="child"
        )
    elif relationship_name == "tasks":
        row = Task(
            id=f"cascade-{uuid_pkg.uuid4().hex[:12]}",
            user_id=user_id,
            media_file_id=media_file.id,
            task_type="transcription",
            status="completed",
        )
    elif relationship_name == "analytics":
        row = Analytics(
            uuid=uuid_pkg.uuid4(), media_file_id=media_file.id, overall_analytics={"child": True}
        )
    elif relationship_name == "file_tags":
        tag = Tag(uuid=uuid_pkg.uuid4(), name=f"c-{uuid_pkg.uuid4().hex[:8]}", user_id=user_id)
        db.add(tag)
        db.flush()
        row = FileTag(uuid=uuid_pkg.uuid4(), media_file_id=media_file.id, tag_id=tag.id)
    elif relationship_name == "collection_memberships":
        collection = Collection(
            uuid=uuid_pkg.uuid4(), name=f"c-{uuid_pkg.uuid4().hex[:8]}", user_id=user_id
        )
        db.add(collection)
        db.flush()
        row = CollectionMember(
            uuid=uuid_pkg.uuid4(), collection_id=collection.id, media_file_id=media_file.id
        )
    elif relationship_name == "speakers":
        row = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="SPEAKER_00",
            user_id=user_id,
            media_file_id=media_file.id,
        )
    elif relationship_name == "topic_suggestions":
        row = TopicSuggestion(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            user_id=user_id,
            suggested_tags=[{"name": "c"}],
        )
    elif relationship_name == "facts_row":
        row = FileFacts(
            media_file_id=media_file.id,
            generator_version="1.1.1",
            source_fingerprint="0" * 64,
            language="en",
            facts={},
            digest={},
            keyphrases={},
            digest_word_count=0,
            section_count=0,
        )
    else:  # pragma: no cover - a new relationship must be added to the map above
        raise AssertionError(f"no seeder for MediaFile.{relationship_name}")
    db.add(row)
    db.flush()
    return row


def test_the_child_map_matches_the_declared_relationships():
    """Guard the guard: the map below must BE ``MediaFile``'s delete-orphan set.

    Derived from the mapper and compared for set equality, so a ninth
    ``delete-orphan`` relationship fails here — with a message telling the author to add
    a seeder — instead of quietly not being covered by any of the tests in this module.
    """
    declared = {
        rel.key for rel in sa_inspect(MediaFile).relationships if "delete-orphan" in rel.cascade
    }
    assert declared == set(_MEDIA_FILE_CHILDREN), (
        f"MediaFile's delete-orphan relationships are {sorted(declared)}; this module "
        f"covers {sorted(_MEDIA_FILE_CHILDREN)}. Add a branch to _seed_child and an "
        "entry to _MEDIA_FILE_CHILDREN saying whether the DB cascades too."
    )


@pytest.mark.parametrize("relationship_name", sorted(_MEDIA_FILE_CHILDREN))
def test_an_instance_delete_sweeps_every_media_file_child(db_session, relationship_name):
    """``db.delete(file)`` removes all eight kinds of child.

    This is the shape ``purge_media_file`` uses, i.e. the one behind every interactive
    delete, the retention sweep and GDPR erasure. For the four NO ACTION children the
    ``cascade=`` keyword is the *only* thing making it work — drop it and the delete
    starts failing outright, which is the failure mode this parametrisation catches per
    relationship rather than in aggregate.
    """
    model, _ = _MEDIA_FILE_CHILDREN[relationship_name]
    user = make_user(db_session, "cascade-owner")
    media_file = make_media_file(db_session, int(user.id))
    child = _seed_child(db_session, media_file, relationship_name)
    child_pk = child.id
    assert db_session.query(model).filter(model.id == child_pk).count() == 1

    db_session.delete(media_file)
    db_session.flush()

    assert db_session.query(model).filter(model.id == child_pk).count() == 0


@pytest.mark.parametrize("relationship_name", sorted(_NO_ACTION_CONSTRAINTS))
def test_a_bulk_delete_is_refused_by_each_no_action_child(db_session, relationship_name):
    """The undocumented half: a bulk parent delete fires NO ORM cascade.

    ``query(MediaFile).delete()`` emits one statement and loads no instances, so
    ``delete-orphan`` never runs and the database's ``NO ACTION`` rule refuses. In
    ``admin._delete_user_media_files`` this surfaces as
    ``500 "User deletion failed"`` with no indication of which constraint objected —
    which is exactly how the missing ``comment`` and ``task`` passes went unnoticed.
    """
    user = make_user(db_session, "cascade-owner")
    media_file = make_media_file(db_session, int(user.id))
    _seed_child(db_session, media_file, relationship_name)

    with pytest.raises(IntegrityError) as excinfo:
        db_session.query(MediaFile).filter(MediaFile.id == media_file.id).delete(
            synchronize_session=False
        )
        db_session.flush()

    assert _NO_ACTION_CONSTRAINTS[relationship_name] in str(excinfo.value)
    db_session.rollback()


@pytest.mark.parametrize(
    "relationship_name",
    sorted(k for k, (_, db_cascades) in _MEDIA_FILE_CHILDREN.items() if db_cascades),
)
def test_a_bulk_delete_sweeps_each_cascading_child(db_session, relationship_name):
    """The control for the test above, and the reason the split is real rather than luck.

    The same bulk statement that is refused for four children succeeds for the other
    four, because those FKs carry ``ON DELETE CASCADE`` and the database does the work.
    Without this half, "bulk delete raises" would read as "bulk delete never works".
    """
    model, _ = _MEDIA_FILE_CHILDREN[relationship_name]
    user = make_user(db_session, "cascade-owner")
    media_file = make_media_file(db_session, int(user.id))
    child = _seed_child(db_session, media_file, relationship_name)
    child_pk = child.id

    db_session.query(MediaFile).filter(MediaFile.id == media_file.id).delete(
        synchronize_session=False
    )
    db_session.flush()

    assert db_session.query(model).filter(model.id == child_pk).count() == 0


#: ``User`` child -> the model. Every one of these ten FKs is ``ON DELETE CASCADE`` in the
#: database as well, so ``User`` does NOT have ``MediaFile``'s split — asserted by
#: :func:`test_every_user_child_is_cascaded_by_the_database_too` rather than assumed.
_USER_CHILDREN: dict[str, Any] = {
    "asr_settings": UserASRSettings,
    "diarization_settings": UserDiarizationSettings,
    "media_sources": UserMediaSource,
    "custom_vocabulary": CustomVocabulary,
    "refresh_tokens": RefreshToken,
    "mfa": UserMFA,
    "password_history": PasswordHistory,
    "org_memberships": OrganizationMembership,
    "owned_groups": UserGroup,
    "group_memberships": UserGroupMember,
}


def _seed_user_child(db, user, relationship_name: str) -> Any:
    """Create exactly ONE child of ``user`` on the named relationship."""
    row: Any
    suffix = uuid_pkg.uuid4().hex[:8]
    if relationship_name == "asr_settings":
        row = UserASRSettings(
            user_id=user.id, name=f"asr-{suffix}", provider="local", model_name="large-v3"
        )
    elif relationship_name == "diarization_settings":
        row = UserDiarizationSettings(
            user_id=user.id, name=f"dia-{suffix}", provider="pyannote", model_name="3.1"
        )
    elif relationship_name == "media_sources":
        row = UserMediaSource(user_id=user.id, hostname=f"host-{suffix}.example.com")
    elif relationship_name == "custom_vocabulary":
        row = CustomVocabulary(user_id=user.id, term=f"term-{suffix}")
    elif relationship_name == "refresh_tokens":
        row = RefreshToken(
            user_id=user.id,
            token_hash=f"hash-{suffix}",
            jti=f"jti-{suffix}",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    elif relationship_name == "mfa":
        row = UserMFA(user_id=user.id, totp_secret=f"secret-{suffix}")
    elif relationship_name == "password_history":
        row = PasswordHistory(user_id=user.id, password_hash=f"phash-{suffix}")
    elif relationship_name == "org_memberships":
        org = Organization(
            uuid=uuid_pkg.uuid4(), external_org_id=f"org-{suffix}", name=f"Org {suffix}"
        )
        db.add(org)
        db.flush()
        row = OrganizationMembership(organization_id=org.id, user_id=user.id)
    elif relationship_name == "owned_groups":
        row = UserGroup(uuid=uuid_pkg.uuid4(), name=f"group-{suffix}", owner_id=user.id)
    elif relationship_name == "group_memberships":
        group = UserGroup(uuid=uuid_pkg.uuid4(), name=f"g-{suffix}", owner_id=user.id)
        db.add(group)
        db.flush()
        row = UserGroupMember(uuid=uuid_pkg.uuid4(), group_id=group.id, user_id=user.id)
    else:  # pragma: no cover - a new relationship must be added to the map above
        raise AssertionError(f"no seeder for User.{relationship_name}")
    db.add(row)
    db.flush()
    return row


def test_the_user_child_map_matches_the_declared_relationships():
    """Guard the guard, as for ``MediaFile``: an eleventh child fails here.

    ``v386`` added four foreign keys into ``user`` in one revision. A future one adding
    a ``delete-orphan`` relationship gets covered because this set equality forces a
    seeder to be written, not because somebody remembered.
    """
    from app.models.user import User

    declared = {rel.key for rel in sa_inspect(User).relationships if "delete-orphan" in rel.cascade}
    assert declared == set(_USER_CHILDREN), (
        f"User's delete-orphan relationships are {sorted(declared)}; this module covers "
        f"{sorted(_USER_CHILDREN)}. Add a branch to _seed_user_child."
    )


@pytest.mark.parametrize("relationship_name", sorted(_USER_CHILDREN))
def test_an_instance_delete_sweeps_every_user_child(db_session, relationship_name):
    """``db.delete(user)`` removes all ten kinds of child.

    Both deletion paths end in ``db.delete(user)``, so this is the last step of every
    account removal — and the one that carries the credential material (``refresh_token``,
    ``user_mfa``, ``password_history``). A child left behind there is not an orphan row,
    it is a live session or a TOTP secret belonging to an account that no longer exists.
    """
    model = _USER_CHILDREN[relationship_name]
    user = make_user(db_session, "cascade-parent")
    child = _seed_user_child(db_session, user, relationship_name)
    child_pk = child.id
    assert db_session.query(model).filter(model.id == child_pk).count() == 1

    db_session.delete(user)
    db_session.flush()

    assert db_session.query(model).filter(model.id == child_pk).count() == 0


@pytest.mark.parametrize("relationship_name", sorted(_USER_CHILDREN))
def test_every_user_child_is_cascaded_by_the_database_too(db_session, relationship_name):
    """``User`` has no ORM/DB split, and that is worth pinning rather than assuming.

    ``MediaFile`` has one for four of its eight children, which is what makes its bulk
    delete path fragile. If a future revision added a ``user_id`` FK with ``NO ACTION``
    under a ``delete-orphan`` relationship, ``db.delete(user)`` would still work (the ORM
    cascade covers it) while any bulk path would break — the exact latent shape this
    asserts is absent.
    """
    from sqlalchemy import text

    model = _USER_CHILDREN[relationship_name]
    rule = (
        db_session.connection()
        .execute(
            text(
                "SELECT c.confdeltype FROM pg_constraint c "
                "WHERE c.contype = 'f' AND c.conrelid = CAST(:tbl AS regclass) "
                "  AND c.confrelid = '\"user\"'::regclass"
            ),
            {"tbl": model.__tablename__},
        )
        .scalars()
        .all()
    )

    assert "c" in rule, (
        f"{model.__tablename__} declares delete-orphan on User but its FK into user is "
        f"{rule} (Postgres codes: c=CASCADE, a=NO ACTION, n=SET NULL) — the ORM would be "
        "the only thing sweeping it, so a bulk delete of the parent would fail"
    )
