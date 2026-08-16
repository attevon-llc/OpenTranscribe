"""A subtitle export must not ship a transcript whose redaction scan is incomplete.

``SubtitleService`` masks with ``seg.redactions or []`` — the **cached** spans.
On a file whose scan never ran, is still queued, or is mid-flight, that list is
empty, so ``mask_segment`` masks nothing and the export writes the raw transcript
to disk. Nothing about the resulting file says the scan was incomplete: an absent
span list and a genuinely clean one are the same value.

This is the sixth instance of that shape in this codebase, and the fifth read
surface to be gated. The other two transcript read paths already refuse:

* ``GET /files/{uuid}`` and ``GET /files/{uuid}/segments`` withhold the segments
  via ``_redaction_pending`` (``files/crud.py``);
* chat's cached-span masker returns ``None`` for any non-``done`` status and the
  caller falls through to inline fail-closed masking.

The export was covered by neither, and commit 8dbb8965 sharpened that: a segment
edit during a detector outage now leaves a previously-``done`` file at
``pending``, so ``pending`` is a *steady state* on a file with a full transcript
and stale spans — precisely the file an export would have shipped unmasked.

The disposition here is `_redaction_pending`'s, not a new one, so the export and
the transcript view cannot drift: ``done``/``failed`` proceed, ``pending`` and
``processing`` are withheld, and a never-scanned file is withheld *and* has its
scan dispatched so the retry can succeed. ``failed`` proceeding is deliberate and
pinned below — the view already shows a failed-scan transcript, so withholding
only the download would make the export stricter than the page it exports.

No Presidio here: these tests write the cached spans directly, which is what a
completed scan leaves behind, and never invoke a detector.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.core import constants as C  # noqa: N812
from app.models.media import MediaFile
from app.models.media import TranscriptSegment

PROFANITY = "damn"
CLEAN_TEXT = "the quarterly numbers look fine"
SENSITIVE_TEXT = f"this is a {PROFANITY} mess and here is more of it"


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


@pytest.fixture
def redacting_user(db_session, normal_user):
    """An owner who turned redaction on with the default categories."""
    _set_prefs(
        db_session,
        normal_user,
        redaction_enabled="true",
        redaction_categories='["profanity", "custom"]',
    )
    return normal_user


@pytest.fixture
def queued_scans(monkeypatch):
    """Capture ``redaction_detect_task.delay`` — this suite has no broker."""
    from app.tasks import redaction_task

    calls: list[dict] = []
    monkeypatch.setattr(
        redaction_task.redaction_detect_task,
        "delay",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def _make_file(db_session, owner, *, redaction_status: str | None) -> MediaFile:
    file_uuid = str(uuid_pkg.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="export_gate.wav",
        title="export_gate",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
        redaction_status=redaction_status,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _add_segments(db_session, media_file, *, with_cached_spans: bool) -> None:
    """Two segments; the sensitive one optionally carries a completed scan's spans."""
    start = SENSITIVE_TEXT.find(PROFANITY)
    spans = (
        [
            {
                "char_start": start,
                "char_end": start + len(PROFANITY),
                "category": "profanity",
                "entity_type": "PROFANITY",
                "detector": "wordlist",
                "confidence": 1.0,
            }
        ]
        if with_cached_spans
        else None
    )
    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id, start_time=0.0, end_time=1.5, text=CLEAN_TEXT
        )
    )
    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            start_time=1.5,
            end_time=3.0,
            text=SENSITIVE_TEXT,
            redactions=spans,
        )
    )
    db_session.commit()


def _export(client, headers, media_file, fmt: str = "srt"):
    return client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=headers,
        params={"subtitle_format": fmt},
    )


# ---------------------------------------------------------------------------
# Withheld: the scan is not complete, so the cached spans are not trustworthy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["srt", "webvtt", "txt"])
@pytest.mark.parametrize(
    "redaction_status", [C.REDACTION_STATUS_PENDING, C.REDACTION_STATUS_PROCESSING]
)
def test_an_incomplete_scan_withholds_the_export(
    client, user_token_headers, redacting_user, db_session, queued_scans, fmt, redaction_status
):
    """Every export format refuses while spans may be missing or stale."""
    media_file = _make_file(db_session, redacting_user, redaction_status=redaction_status)
    _add_segments(db_session, media_file, with_cached_spans=False)

    response = _export(client, user_token_headers, media_file, fmt)

    # Asserted before the status code on purpose: the property that matters is
    # that no transcript text left the building, and a failure here should say
    # what escaped rather than just naming a number.
    assert PROFANITY not in response.text
    assert CLEAN_TEXT not in response.text
    assert response.status_code == status.HTTP_409_CONFLICT


def test_the_withheld_response_says_why_and_that_it_is_retryable(
    client, user_token_headers, redacting_user, db_session, queued_scans
):
    """A 409 that reads as 'permanently unavailable' would send callers away."""
    media_file = _make_file(db_session, redacting_user, redaction_status=C.REDACTION_STATUS_PENDING)
    _add_segments(db_session, media_file, with_cached_spans=False)

    detail = _export(client, user_token_headers, media_file).json()["detail"].lower()

    assert "redaction" in detail
    assert "again" in detail or "retry" in detail


def test_a_never_scanned_file_is_withheld_and_has_its_scan_dispatched(
    client, user_token_headers, redacting_user, db_session, queued_scans
):
    """A NULL status means no one ever asked; withholding forever would be a dead end."""
    media_file = _make_file(db_session, redacting_user, redaction_status=None)
    _add_segments(db_session, media_file, with_cached_spans=False)

    response = _export(client, user_token_headers, media_file)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert queued_scans, "the export withheld but never queued the scan it is waiting for"
    db_session.refresh(media_file)
    assert media_file.redaction_status == C.REDACTION_STATUS_PENDING


def test_an_owner_reveal_request_cannot_bypass_the_gate(
    client, user_token_headers, redacting_user, db_session, queued_scans
):
    """``?redact=false`` reveals *masked* categories; it does not un-gate an unscanned file.

    The reveal path is the owner asking for their own originals, which is
    legitimate — but on a file whose scan never completed there is nothing to
    reveal *from*, and honoring it here would make the gate opt-out via a query
    parameter.
    """
    media_file = _make_file(
        db_session, redacting_user, redaction_status=C.REDACTION_STATUS_PROCESSING
    )
    _add_segments(db_session, media_file, with_cached_spans=False)

    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"redact": "false"},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert PROFANITY not in response.text


# ---------------------------------------------------------------------------
# Allowed: the same dispositions the transcript view already takes
# ---------------------------------------------------------------------------


def test_a_completed_scan_exports_with_its_cached_spans_applied(
    client, user_token_headers, redacting_user, db_session, queued_scans
):
    """The gate must not break the case it exists to protect."""
    media_file = _make_file(db_session, redacting_user, redaction_status=C.REDACTION_STATUS_DONE)
    _add_segments(db_session, media_file, with_cached_spans=True)

    response = _export(client, user_token_headers, media_file)

    assert response.status_code == status.HTTP_200_OK
    assert CLEAN_TEXT in response.text
    assert PROFANITY not in response.text, "cached span was not applied to the export"


def test_a_failed_scan_still_exports_matching_the_transcript_view(
    client, user_token_headers, redacting_user, db_session, queued_scans
):
    """Pinned: ``_redaction_pending`` returns False for ``failed``, so the page renders.

    Withholding only the download would make the export stricter than the view it
    exports, and a permanently-failed scan would make the file undownloadable with
    no operator remedy. If that disposition is ever revisited it must move in both
    places at once, which is what this test enforces.
    """
    media_file = _make_file(db_session, redacting_user, redaction_status=C.REDACTION_STATUS_FAILED)
    _add_segments(db_session, media_file, with_cached_spans=True)

    response = _export(client, user_token_headers, media_file)

    assert response.status_code == status.HTTP_200_OK
    assert CLEAN_TEXT in response.text


def test_redaction_disabled_exports_regardless_of_scan_status(
    client, user_token_headers, normal_user, db_session, queued_scans
):
    """With no policy to enforce there is nothing to withhold — and no lazy dispatch."""
    media_file = _make_file(db_session, normal_user, redaction_status=None)
    _add_segments(db_session, media_file, with_cached_spans=False)

    response = _export(client, user_token_headers, media_file)

    assert response.status_code == status.HTTP_200_OK
    assert PROFANITY in response.text
    assert not queued_scans
