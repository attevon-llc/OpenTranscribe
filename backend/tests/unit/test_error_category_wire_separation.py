"""Issue #843 — split the retry-policy `error_category` from the user-facing wire field.

Two enums used to share the same name and colliding values: the retry-policy
`app.utils.error_classification.ErrorCategory` (persisted on `media_file.error_category`) and
the user-facing `app.services.error_categorization_service.UserErrorReason` (served over the
wire as `error_category` in the old schema). This module pins that:

- the Pydantic wire schema exposes `error_reason`, never `error_category` — so the retry-policy
  column can no longer reach a client under a name that looked like it already did;
- a retry category set on a non-errored row never leaks into a formatted response;
- a retry clears the stale category alongside the message; and
- the two vocabularies no longer collide by value.
"""

from __future__ import annotations

import datetime
import uuid

from app.models.media import FileStatus
from app.models.media import MediaFile as MediaFileModel
from app.models.user import User
from app.schemas.media import MediaFile as MediaFileSchema
from app.services.error_categorization_service import UserErrorReason
from app.services.formatting_service import FormattingService
from app.utils.error_classification import ErrorCategory
from app.utils.task_utils import reset_file_for_retry


def test_the_retry_vocabulary_is_not_a_wire_field():
    assert "error_category" not in MediaFileSchema.model_fields
    assert "error_reason" in MediaFileSchema.model_fields


def test_a_retry_category_on_a_non_errored_row_never_reaches_the_wire():
    media_file = MediaFileModel()
    media_file.id = 1
    media_file.uuid = uuid.uuid4()
    owner_user = User()
    owner_user.uuid = uuid.uuid4()
    media_file.user = owner_user
    media_file.filename = "meeting.mp4"
    media_file.storage_path = "user/1/meeting.mp4"
    media_file.upload_time = datetime.datetime.now(datetime.UTC)
    media_file.status = FileStatus.PENDING
    media_file.error_category = "worker_lost"
    media_file.last_error_message = None

    result = FormattingService.format_media_file(media_file, None)
    dumped = result.model_dump_json()

    reasons = list(ErrorCategory)
    assert len(reasons) > 0, "ErrorCategory is empty — the loop below would pass vacuously"
    for reason in reasons:
        assert reason.value not in dumped, (
            f"retry-policy value {reason.value!r} leaked onto the wire for a PENDING file"
        )


def test_reset_file_for_retry_clears_the_error_category(db_session, normal_user):
    media_file = MediaFileModel(
        uuid=str(uuid.uuid4()),
        filename=f"retry_{uuid.uuid4().hex[:8]}.wav",
        title="retry clears category",
        storage_path=f"user/test/retry_{uuid.uuid4().hex[:8]}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=FileStatus.ERROR,
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
        error_category="worker_lost",
        last_error_message="Worker lost connection",
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    ok = reset_file_for_retry(db_session, media_file.id)

    assert ok is True
    db_session.expire_all()
    refreshed = db_session.query(MediaFileModel).filter(MediaFileModel.id == media_file.id).one()
    assert refreshed.last_error_message is None
    assert refreshed.error_category is None


def test_the_two_vocabularies_do_not_share_a_value():
    retry_values = {e.value for e in ErrorCategory}
    user_facing_values = {r.value for r in UserErrorReason}

    assert retry_values & user_facing_values == set()
