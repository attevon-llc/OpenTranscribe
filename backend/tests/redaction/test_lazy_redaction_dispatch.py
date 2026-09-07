"""A never-scanned transcript must not become PERMANENTLY unreadable.

``media_file.redaction_status = NULL`` is the deliberate fail-closed state for a
file whose detection scan never ran, and the gate that reads it withholds the
transcript for NULL exactly as it does for ``pending``. **That equivalence is
load-bearing and must not be relaxed**: the status rule lives in
``services/redaction/export_policy.export_masking_is_pending``, which the
transcript read (``crud._redaction_pending``), the single-file subtitle
download, the bulk-export ZIP and the burned-in-subtitle render all share. A
"fix" that let NULL through there would hand three export surfaces a transcript
no detector ever looked at, which is issue #85 reintroduced.

The remedy for NULL is therefore not a weaker gate, it is
``_lazy_dispatch_redaction``: the first read of an unscanned file queues the
scan, and the file becomes readable a few seconds later. Measured on the dev
stack 2026-09-07 — an injected transcript went ``NULL`` -> ``pending`` ->
``processing`` -> ``done`` and served all 6 of its segments ~25 s after the
first ``GET /segments``.

**That remedy has a hole, and this file is about the hole.** The helper commits
``pending`` *before* publishing to the broker and swallows a publish failure. A
broker that is unreachable — ``SKIP_REDIS`` in the E2E process, a Redis
restarting under a rebuild, a transient network fault — therefore leaves the row
at ``pending`` with nothing running and nothing that ever will: ``pending`` is
not NULL, so no later read re-dispatches. The transcript is withheld forever,
from an error no user is shown. Writing the status first is correct (it is what
stops two concurrent readers queueing two scans); not undoing it when the
dispatch fails is not.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core import constants as C  # noqa: N812


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


@pytest.fixture
def redacting_user(db_session, normal_user):
    """An owner who turned redaction on — the only case the gate applies to."""
    _set_prefs(db_session, normal_user, redaction_enabled="true")
    return normal_user


@pytest.fixture
def unscanned_file(db_session, redacting_user):
    """A COMPLETED transcript whose detection scan has never run.

    The shape every non-pipeline writer produces: ``app.scripts.corpus_injection``
    writes exactly this, and so does any deployment that enabled redaction after
    its library already existed.
    """
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=redacting_user.id,
        filename=f"never-scanned-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"lazy-dispatch-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language="en",
        status=FileStatus.COMPLETED,
        redaction_status=None,
    )
    db_session.add(media)
    db_session.flush()
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            start_time=0.0,
            end_time=5.0,
            text="there is a whole transcript here that nobody can read",
        )
    )
    db_session.flush()
    return media


@pytest.fixture
def broker(monkeypatch):
    """Stand in for ``redaction_detect_task.delay``; publishing can be made to fail.

    Not a convenience: an unreachable broker is the *only* condition that
    produces the permanent-withholding bug, and there is no way to reach it
    through a working stack.
    """
    from app.tasks import redaction_task

    class _Broker:
        def __init__(self) -> None:
            self.published: list[dict] = []
            self.reachable = True

        def delay(self, **kwargs):
            if not self.reachable:
                raise OSError("[Errno 111] Connection refused: redis://redis:6379//")
            self.published.append(kwargs)
            return object()

    stub = _Broker()
    monkeypatch.setattr(redaction_task.redaction_detect_task, "delay", stub.delay)
    return stub


def _read_gate(db_session, user, media) -> bool:
    """Ask the real transcript-read gate whether this file is withheld."""
    from app.api.endpoints.files.crud import _redaction_pending
    from app.services.redaction.config import resolve_effective_config

    return _redaction_pending(db_session, resolve_effective_config(db_session, user.id), media)


# ----------------------------------------------------- the rule that must NOT change


def test_a_null_status_is_withheld_exactly_like_pending(db_session, redacting_user, unscanned_file):
    """NULL and ``pending`` are one state to every reader of the status rule.

    Pinned here because the tempting repair for "my injected corpus is invisible"
    is to teach the gate that NULL means "never scanned, let it through". It does
    mean never scanned — which is precisely why it is withheld.
    """
    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.export_policy import export_masking_is_pending

    cfg = resolve_effective_config(db_session, redacting_user.id)
    assert export_masking_is_pending(cfg, None) is True, (
        "an unexamined transcript must not reach an export surface"
    )
    assert export_masking_is_pending(cfg, C.REDACTION_STATUS_PENDING) is True
    assert export_masking_is_pending(cfg, C.REDACTION_STATUS_DONE) is False
    assert export_masking_is_pending(cfg, C.REDACTION_STATUS_FAILED) is False


def test_reading_an_unscanned_transcript_queues_the_scan_that_makes_it_readable(
    db_session, redacting_user, unscanned_file, broker
):
    """The control: with a reachable broker, NULL is a transient state."""
    assert _read_gate(db_session, redacting_user, unscanned_file) is True

    assert [call["file_id"] for call in broker.published] == [unscanned_file.id]
    db_session.refresh(unscanned_file)
    assert unscanned_file.redaction_status == C.REDACTION_STATUS_PENDING, (
        "the status is claimed up front so a second concurrent reader does not queue a second scan"
    )


# ------------------------------------------------ the hole: a dispatch that never lands


def test_a_dispatch_that_never_reached_the_broker_leaves_the_file_re_dispatchable(
    db_session, redacting_user, unscanned_file, broker
):
    """A failed publish must not strand the row at ``pending``.

    ``pending`` asserts "a scan is coming". When the publish raised, none is, and
    nothing else in the system moves a file off ``pending`` — ``detect_and_store``
    is the only writer of a terminal status and it is exactly what did not get
    queued. So the claim has to be withdrawn, or the transcript is unreadable for
    the life of the row.
    """
    broker.reachable = False

    assert _read_gate(db_session, redacting_user, unscanned_file) is True, (
        "the read is still withheld — this is fail-closed and correct"
    )

    db_session.refresh(unscanned_file)
    # Asserted as the exact expected VALUE, not as "not pending": NULL is the only
    # status `_redaction_pending` re-dispatches from, so `failed` or a half-written
    # `processing` would satisfy a negated assertion and still strand the file.
    assert unscanned_file.redaction_status is None, (
        f"expected the status to return to NULL — the one value the read gate "
        f"re-dispatches from — but it is {unscanned_file.redaction_status!r}. At "
        "`pending` the row claims a scan is queued when the publish failed and none "
        "is; nothing will ever move it off, so this transcript is withheld forever "
        "from an error the user never sees."
    )


def test_the_next_read_after_a_failed_dispatch_queues_the_scan(
    db_session, redacting_user, unscanned_file, broker
):
    """Recovery is automatic once the broker is back — no operator step.

    This is the property the assertion above exists to buy, and the reason
    restoring the *previous* status matters rather than picking any non-pending
    value: only NULL makes ``_redaction_pending`` dispatch again.
    """
    broker.reachable = False
    _read_gate(db_session, redacting_user, unscanned_file)
    assert broker.published == []

    broker.reachable = True
    assert _read_gate(db_session, redacting_user, unscanned_file) is True
    assert [call["file_id"] for call in broker.published] == [unscanned_file.id], (
        "the retry the user gets for free — reloading the page — must queue the scan"
    )
