"""Regression test for the rediarize pandas-3.0 crash.

Measured against a real stack running current code: ``rediarize_task`` failed for every
file with pandas 3.0's ``Multi-dimensional indexing (e.g. `obj[:, None]`) is no longer
supported`` — and did so *quietly*, because the task catches the exception and returns
``{"status": "error", ...}`` rather than raising, so Celery reports the task itself as
"succeeded". A test that only checks the task ran without raising would pass against the
bug; this asserts the actual outcome.

Root cause (confirmed by direct reproduction against the real pandas 3.0.5 install):
``app.transcription.speaker_assigner.assign_speakers``/``_batch_assign`` typed their
diarization input as ``DiarizeResult`` (numpy arrays) but never defensively converted
``.start``/``.end``/``.speaker`` to ``ndarray``. A pandas DataFrame answers the same
attribute names via column access (as ``Series``, not ``ndarray``), and pandas 3.0 hard-errors
the moment ``_batch_assign`` does ``d_ends[None, :]`` on a Series where pandas 2.x only
warned. Fixed by wrapping those three accessors in ``np.asarray()``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pandas as pd


def _make_user(db):
    from app.core.security import get_password_hash
    from app.models.user import User

    user = User(
        email=f"rediarize-pandas3-{uuid.uuid4().hex[:8]}@example.invalid",
        full_name="Rediarize Pandas3 Fixture",
        hashed_password=get_password_hash("throwaway-password-1"),  # noqa: S106
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_file_with_two_speakers(db, user):
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    media_file = MediaFile(
        user_id=user.id,
        filename=f"rediarize-pandas3-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"test/rediarize-pandas3/{uuid.uuid4().hex[:8]}.wav",
        content_type="audio/wav",
        file_size=1000,
        status=FileStatus.COMPLETED,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)

    speaker_a = Speaker(media_file_id=media_file.id, user_id=user.id, name="SPEAKER_00")
    speaker_b = Speaker(media_file_id=media_file.id, user_id=user.id, name="SPEAKER_01")
    db.add_all([speaker_a, speaker_b])
    db.commit()
    db.refresh(speaker_a)
    db.refresh(speaker_b)

    seg1 = TranscriptSegment(
        media_file_id=media_file.id,
        speaker_id=speaker_a.id,
        start_time=0.0,
        end_time=5.0,
        text="the original wording nobody has corrected yet",
    )
    seg2 = TranscriptSegment(
        media_file_id=media_file.id,
        speaker_id=speaker_b.id,
        start_time=5.0,
        end_time=10.0,
        text="a second segment from the other speaker",
    )
    db.add_all([seg1, seg2])
    db.commit()

    return media_file


def test_rediarize_completes_when_the_diarizer_returns_a_dataframe(db_session):
    """The exact shape measured in production: a diarization result whose ``.start``/
    ``.end``/``.speaker`` are pandas Series (not numpy arrays). Before the fix this made
    the task return ``{"status": "error", "message": "Multi-dimensional indexing..."}}``
    with every real file; after the fix it must return ``{"status": "success", ...}``."""

    @contextmanager
    def _test_session_scope():
        yield db_session
        db_session.commit()

    user = _make_user(db_session)
    media_file = _make_file_with_two_speakers(db_session, user)
    file_uuid = str(media_file.uuid)

    diarize_df = pd.DataFrame(
        [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_00"},
        ]
    )

    from app.tasks import rediarize_task as mod

    with (
        # rediarize_task.py does `from app.db.session_utils import session_scope`, so its
        # module-level name is bound at import time — patching the source module's attribute
        # would not affect it. Patch the name where the task actually looks it up.
        patch.object(mod, "session_scope", _test_session_scope),
        patch.object(mod, "_prepare_audio", return_value=("/tmp/fake.wav", None)),  # noqa: S108
        patch.object(mod, "_run_diarization", return_value=(diarize_df, {"regions": []}, None)),
        patch("app.tasks.transcription.notifications.send_progress_notification"),
        patch("app.tasks.transcription.notifications.send_completion_notification"),
        patch(
            "app.tasks.speaker_attribute_task._is_speaker_attribute_detection_enabled",
            return_value=False,
        ),
        patch("app.tasks.transcription.core._process_speaker_embeddings"),
        patch("app.tasks.transcription.core._should_use_native_embeddings", return_value=False),
    ):
        result = mod.rediarize_task.apply(
            kwargs={"file_uuid": file_uuid, "downstream_tasks": None}
        ).result

    assert result["status"] == "success", (
        f"rediarize must complete, not report an error payload while Celery calls the "
        f"task itself successful: got {result!r}"
    )
    assert result["segments"] == 2
    assert result["speakers"] == 1
