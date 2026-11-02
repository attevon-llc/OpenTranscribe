"""The legal gate runs FIRST in the shared pre-dispatch check, and refused files are not retried.

``_run_pre_dispatch_gate`` is the backstop every dispatch path goes through (single and batch).
A refusal must happen before a registered before-dispatch hook fires (a cloud quota hook must
not reserve anything for a job that will not run), must leave no task row, and must mark the
file ERROR with a category automatic recovery never re-selects.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.constants import LEGAL_PROCESSING_REQUIRED_KEYS_KEY
from app.models.media import MediaFile
from app.models.media import Task
from app.models.system_settings import SystemSettings
from app.tasks.transcription import hooks
from app.tasks.transcription.dispatch import _run_pre_dispatch_gate
from app.tasks.transcription.hooks import LegalAcceptanceRequiredError

pytestmark = pytest.mark.xdist_group("legal_processing_gate_settings")

TERMS = "opentranscribe-terms-of-use"


@pytest.fixture
def media_file(db_session, normal_user):
    row = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="dispatch_gate.wav",
        title="dispatch_gate",
        storage_path="media/test/dispatch_gate.wav",
        content_type="audio/wav",
        file_size=1024,
        status="pending",
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
        duration=60.0,
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def recorded_hook():
    calls: list = []
    hooks.register_before_dispatch(calls.append)
    try:
        yield calls
    finally:
        hooks.clear_hooks()


def _gate_on(db_session) -> None:
    db_session.add(SystemSettings(key=LEGAL_PROCESSING_REQUIRED_KEYS_KEY, value=TERMS))
    db_session.commit()


def test_refusal_is_raised_before_any_hook(db_session, media_file, recorded_hook):
    _gate_on(db_session)
    with pytest.raises(LegalAcceptanceRequiredError) as caught:
        _run_pre_dispatch_gate(db_session, media_file, str(media_file.uuid), "task-x")
    assert caught.value.code == "legal_acceptance_required"
    assert caught.value.document_keys == (TERMS,)
    assert recorded_hook == []
    db_session.refresh(media_file)
    assert media_file.status == "error"
    assert media_file.error_category == "legal_acceptance_required"
    assert db_session.query(Task).filter(Task.media_file_id == media_file.id).count() == 0


def test_hooks_still_run_when_the_gate_is_off(db_session, media_file, recorded_hook):
    _run_pre_dispatch_gate(db_session, media_file, str(media_file.uuid), "task-y")
    assert len(recorded_hook) == 1
    assert recorded_hook[0].file_id == media_file.id


def test_the_refusal_is_a_dispatch_blocked_error_with_a_dict_detail():
    err = LegalAcceptanceRequiredError("legal_acceptance_required", (TERMS,))
    assert isinstance(err, hooks.DispatchBlockedError)
    assert err.status_code == 403
    assert err.message == f"legal_acceptance_required: {TERMS}"


def test_recovery_never_reselects_a_refused_file(db_session, normal_user):
    """``check_for_stuck_files`` picks ERROR files for retry; a refused one is excluded."""
    from app.utils.task_utils import check_for_stuck_files

    refused = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="refused.wav",
        storage_path="media/test/refused.wav",
        content_type="audio/wav",
        file_size=1,
        status="error",
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
        error_category="legal_acceptance_required",
    )
    other = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="other.wav",
        storage_path="media/test/other.wav",
        content_type="audio/wav",
        file_size=1,
        status="error",
        is_public=False,
        retry_count=0,
        user_id=normal_user.id,
        error_category="network",
    )
    db_session.add_all([refused, other])
    db_session.commit()
    selected = set(check_for_stuck_files(db_session))
    assert other.id in selected  # control: an ordinary failed file is picked up
    assert refused.id not in selected


def test_the_refusal_category_is_permanent():
    from app.utils.error_classification import ErrorCategory
    from app.utils.error_classification import should_retry

    assert not should_retry(ErrorCategory.LEGAL_ACCEPTANCE_REQUIRED, 0)
