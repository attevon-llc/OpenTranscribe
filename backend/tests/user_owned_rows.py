"""Seed a user who actually OWNS things, so a deletion test can fail.

Why this exists
---------------
``tests/api/endpoints/test_admin.py::test_admin_users_delete`` and
``tests/api/endpoints/test_users.py::test_delete_user`` both deleted the bare
``normal_user`` fixture — an account with no media files, no segments, no
speakers, no comments, no tags and no tasks.

``admin._delete_user_owned_records`` and ``admin._delete_user_media_files`` are
hand-maintained lists of the foreign keys that have **no DB-level CASCADE**, and
every branch in them is shaped ``if <ids>: <delete>``. With nothing owned, every
single branch was skipped, ``db.delete(user)`` succeeded because no child row
referenced the account, and both tests passed and would have kept passing if the
bodies of those two functions were replaced with ``pass``. Two tests, zero
coverage of the code they name.

So the fixture below creates one row in every table the two helpers claim to
sweep, plus the three cross-account rows a same-owner seed cannot produce
(another user's comment on the subject's file, the subject's comment on another
user's file, and the subject as ``shared_by`` on another user's prompt).
:meth:`OwnedRows.remaining` then reports what is still present, so the assertion
is ``remaining == {}`` with a failure message that names the leaking tables
instead of a bare ``assert not None``.

:meth:`OwnedRows.remaining` is only meaningful next to
:meth:`OwnedRows.assert_all_present`, which is the **control**: it proves the
rows were really there before the deletion ran. Without it, a seed helper that
silently failed to insert anything would make ``remaining == {}`` pass for the
same reason the original tests passed.
"""

from __future__ import annotations

import uuid as uuid_pkg
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.media import Analytics
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import Comment
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCollection
from app.models.media import SpeakerCollectionMember
from app.models.media import SpeakerProfile
from app.models.media import Tag
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.prompt import SummaryPrompt
from app.models.topic import TopicSuggestion
from app.models.user import User


def make_user(db: Session, label: str, *, role: str = "user") -> User:
    """A throwaway account with a random email — never a fixed identity."""
    user = User(
        email=f"{label}-{uuid_pkg.uuid4().hex[:10]}@example.com",
        full_name=f"Deletion Fixture {label}",
        hashed_password=get_password_hash("password123"),  # noqa: S106 — throwaway fixture row
        is_active=True,
        is_superuser=role == "super_admin",
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_media_file(db: Session, user_id: int) -> MediaFile:
    """A minimal COMPLETED media file owned by ``user_id``."""
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"deletion-fixture-{fuuid.hex[:8]}.mp4",
        storage_path=f"media/deletion-fixture/{fuuid}.mp4",
        content_type="video/mp4",
        file_size=1234,
        user_id=user_id,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


@dataclass
class OwnedRows:
    """Ids of everything seeded, plus the two report helpers the tests assert on."""

    user_id: int
    media_file_id: int
    other_user_id: int
    other_media_file_id: int
    tag_id: int
    collection_id: int
    speaker_collection_id: int
    speaker_profile_id: int
    task_id: str
    prompt_id: int
    #: label -> (model, column, value). One entry per table the deletion path claims.
    expectations: dict[str, tuple[type, str, object]]

    def counts(self, db: Session) -> dict[str, int]:
        """Live row count per seeded table, whatever it is."""
        db.expire_all()
        return {
            label: db.query(model).filter(getattr(model, column) == value).count()
            for label, (model, column, value) in self.expectations.items()
        }

    def remaining(self, db: Session) -> dict[str, int]:
        """Only the tables that still hold rows — ``{}`` means the cascade was complete.

        Returning the non-empty subset rather than a bool is deliberate: the failure
        message then names every table that leaked in one run, instead of one per
        debugging cycle.
        """
        return {label: n for label, n in self.counts(db).items() if n}

    def assert_all_present(self, db: Session) -> None:
        """The control. Every seeded table must hold at least one row *before* deletion.

        A deletion assertion is only as good as the proof that there was something to
        delete — which is exactly what the tests this fixture replaces were missing.
        """
        missing = [label for label, n in self.counts(db).items() if n == 0]
        assert missing == [], (
            f"seed did not create rows for {missing}; the post-deletion assertion "
            "would pass vacuously"
        )


def seed_owned_rows(db: Session, user: User) -> OwnedRows:
    """Give ``user`` one row in every table the deletion helpers claim to sweep.

    Includes three rows that cross accounts, because the interesting foreign keys are
    the ones an owner-scoped ``WHERE user_id = :id`` cannot see:

    * ``other_user``'s comment on ``user``'s file — ``comment.media_file_id`` is
      ``ON DELETE NO ACTION`` and commenting is collaborative, so this row blocks the
      bulk ``MediaFile`` delete unless the path scopes comments by file.
    * ``user``'s comment on ``other_user``'s file — ``comment.user_id`` is NOT NULL /
      NO ACTION, so this row blocks ``DELETE FROM "user"`` unless the path scopes
      comments by author too. The two together are why *both* scopings are needed.
    * ``user`` as ``summary_prompt.shared_by`` on ``other_user``'s prompt — an admin
      may flip sharing on somebody else's prompt (``prompts.share_prompt``).

    Returns:
        An :class:`OwnedRows` describing what was created.
    """
    media_file = make_media_file(db, int(user.id))
    other_user = make_user(db, "deletion-bystander")
    other_media_file = make_media_file(db, int(other_user.id))

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        name="SPEAKER_00",
        user_id=user.id,
        media_file_id=media_file.id,
    )
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        name=f"Profile {uuid_pkg.uuid4().hex[:8]}",
    )
    db.add(speaker)
    db.flush()
    # speaker_id set ON PURPOSE. `transcript_segment.speaker_id` is ON DELETE NO ACTION
    # and `_delete_user_speakers` runs BEFORE the segments are removed, so a segment
    # pointing at a speaker is what makes the deletion path fail — and every diarized
    # segment in a real deployment carries one (14,274 of 14,274 in dev). A segment with
    # `speaker_id = NULL` looks like a transcript and exercises none of it.
    segment = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media_file.id,
        speaker_id=speaker.id,
        start_time=0.0,
        end_time=1.5,
        text="seeded segment",
    )
    analytics = Analytics(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media_file.id,
        overall_analytics={"seeded": True},
    )
    tag = Tag(uuid=uuid_pkg.uuid4(), name=f"seed-{uuid_pkg.uuid4().hex[:8]}", user_id=user.id)
    collection = Collection(
        uuid=uuid_pkg.uuid4(), name=f"seed-col-{uuid_pkg.uuid4().hex[:8]}", user_id=user.id
    )
    speaker_collection = SpeakerCollection(
        uuid=uuid_pkg.uuid4(), name=f"seed-sc-{uuid_pkg.uuid4().hex[:8]}", user_id=user.id
    )
    prompt = SummaryPrompt(
        uuid=uuid_pkg.uuid4(),
        name=f"seed-prompt-{uuid_pkg.uuid4().hex[:8]}",
        prompt_text="summarise",
        user_id=user.id,
    )
    topic = TopicSuggestion(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media_file.id,
        user_id=user.id,
        suggested_tags=[{"name": "seed"}],
    )
    db.add_all([profile, segment, analytics, tag, collection, speaker_collection, prompt, topic])
    db.commit()

    task_id = f"seed-task-{uuid_pkg.uuid4().hex[:12]}"
    db.add_all(
        [
            FileTag(uuid=uuid_pkg.uuid4(), media_file_id=media_file.id, tag_id=tag.id),
            CollectionMember(
                uuid=uuid_pkg.uuid4(), collection_id=collection.id, media_file_id=media_file.id
            ),
            SpeakerCollectionMember(
                uuid=uuid_pkg.uuid4(),
                collection_id=speaker_collection.id,
                speaker_profile_id=profile.id,
            ),
            Task(
                id=task_id,
                user_id=user.id,
                media_file_id=media_file.id,
                task_type="transcription",
                status="completed",
            ),
            Comment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                user_id=user.id,
                text="my own comment",
            ),
            # The cross-account rows — see the docstring.
            Comment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                user_id=other_user.id,
                text="a collaborator's comment on the deleted user's file",
            ),
            Comment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=other_media_file.id,
                user_id=user.id,
                text="the deleted user's comment on someone else's file",
            ),
        ]
    )
    other_prompt = SummaryPrompt(
        uuid=uuid_pkg.uuid4(),
        name=f"seed-other-prompt-{uuid_pkg.uuid4().hex[:8]}",
        prompt_text="summarise",
        user_id=other_user.id,
        is_shared=True,
        shared_by=user.id,
    )
    db.add(other_prompt)
    db.commit()

    expectations: dict[str, tuple[type, str, object]] = {
        "media_file": (MediaFile, "user_id", user.id),
        "transcript_segment": (TranscriptSegment, "media_file_id", media_file.id),
        "speaker": (Speaker, "user_id", user.id),
        "speaker_profile": (SpeakerProfile, "user_id", user.id),
        "analytics": (Analytics, "media_file_id", media_file.id),
        "tag": (Tag, "user_id", user.id),
        "file_tag": (FileTag, "media_file_id", media_file.id),
        "collection": (Collection, "user_id", user.id),
        "collection_member": (CollectionMember, "collection_id", collection.id),
        "speaker_collection": (SpeakerCollection, "user_id", user.id),
        "speaker_collection_member": (
            SpeakerCollectionMember,
            "collection_id",
            speaker_collection.id,
        ),
        "summary_prompt": (SummaryPrompt, "user_id", user.id),
        "topic_suggestion": (TopicSuggestion, "user_id", user.id),
        "task": (Task, "user_id", user.id),
        # Keyed by author, so it covers the comment on ANOTHER user's file — the row
        # an owner-scoped per-file sweep cannot reach.
        "comment_by_this_user": (Comment, "user_id", user.id),
        # Keyed by file, so it covers the collaborator's comment on THIS user's file.
        "comment_on_this_users_file": (Comment, "media_file_id", media_file.id),
    }
    return OwnedRows(
        user_id=int(user.id),
        media_file_id=int(media_file.id),
        other_user_id=int(other_user.id),
        other_media_file_id=int(other_media_file.id),
        tag_id=int(tag.id),
        collection_id=int(collection.id),
        speaker_collection_id=int(speaker_collection.id),
        speaker_profile_id=int(profile.id),
        task_id=task_id,
        prompt_id=int(prompt.id),
        expectations=expectations,
    )
