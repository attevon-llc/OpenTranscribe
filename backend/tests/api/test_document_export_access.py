"""Who can ask for a DOCX / PDF export, and what stays refused at run time (issue #1278).

One test per row of the plan's security table. The dispatching route answers what it can
immediately (visibility, capability, support grant, a pending scan); the worker re-applies every
gate when it runs, because a takedown or an admin tightening the floor can land in the gap.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fixtures.document_export import docx_text
from fixtures.redaction_export_world import CLEAN_TEXT
from fixtures.redaction_export_world import PROFANITY
from fixtures.redaction_export_world import enable_user_redaction
from fixtures.redaction_export_world import make_redactable_file
from fixtures.redaction_export_world import set_export_locked

from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.download_events import DOCUMENT_EXPORT_ERRORS
from app.tasks.transcript_document import prepare_transcript_document_task

pytestmark = pytest.mark.xdist_group("redaction_system_settings")

JOB = "a" * 64


@pytest.fixture
def documents_capability_off():
    set_capability_resolver(lambda request: {**COMMUNITY_CAPABILITIES, "exports.documents": False})
    yield
    reset_capability_resolver()


def _run_task(media_file, user_id, **request):
    """Call the task body directly, as a worker would, for a user the route would refuse."""
    return prepare_transcript_document_task.run(
        file_id=media_file.id,
        user_id=user_id,
        organization_id=None,
        request={"format": "docx", **request},
        job_id=JOB,
    )


# --- dispatch-time refusals -----------------------------------------------------------------


def test_a_file_the_user_cannot_see_is_refused_like_the_sync_export(
    client, other_user_auth_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    response = document_export.post(client, other_user_auth_headers, media_file)
    sync = client.get(
        f"/api/files/{media_file.uuid}/export",
        headers=other_user_auth_headers,
        params={"format": "txt"},
    )
    assert response.status_code == sync.status_code == 403
    assert document_export.dispatched == []


def test_another_organizations_file_is_refused(support_world, document_export):
    response = document_export.post(
        support_world.client, support_world.org_admin_headers, support_world.other_org_file
    )
    assert response.status_code == 403
    assert document_export.dispatched == []
    # CONTROL: an owner passes the permission check on their own personal-scope file (it has no
    # transcript, which is the NEXT gate, so that is the answer that proves access was granted)
    own = document_export.post(
        support_world.client, support_world.owner_headers, support_world.personal_file
    )
    assert own.status_code == 404
    assert own.json()["detail"] == "No transcript available for this file"


def test_a_support_grant_cannot_export_content(support_world, document_export):
    grant = support_world.active_grant()
    response = document_export.post(
        support_world.client, support_world.with_grant(grant), support_world.file
    )
    assert response.status_code == 403
    assert document_export.dispatched == []


def test_the_capability_off_makes_the_route_a_404(
    client, user_token_headers, normal_user, db_session, document_export, documents_capability_off
):
    media_file = make_redactable_file(db_session, normal_user)
    response = document_export.post(client, user_token_headers, media_file)
    assert response.status_code == 404
    assert document_export.dispatched == []


def test_the_capability_on_dispatches(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    assert document_export.post(client, user_token_headers, media_file).status_code == 202
    assert len(document_export.dispatched) == 1


def test_a_file_still_scanning_is_409_when_the_policy_masks(
    client, user_token_headers, normal_user, db_session, document_export
):
    enable_user_redaction(client, user_token_headers)
    media_file = make_redactable_file(db_session, normal_user)
    media_file.redaction_status = "processing"
    db_session.commit()
    response = document_export.post(client, user_token_headers, media_file)
    assert response.status_code == 409
    assert document_export.dispatched == []


def test_a_file_not_yet_transcribed_is_400(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    media_file.status = "processing"
    db_session.commit()
    assert document_export.post(client, user_token_headers, media_file).status_code == 400


def test_a_file_with_no_transcript_is_404(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    db_session.query(TranscriptSegment).filter_by(media_file_id=media_file.id).delete()
    db_session.commit()
    assert document_export.post(client, user_token_headers, media_file).status_code == 404


def test_an_oversized_label_is_422_and_a_boundary_label_is_accepted(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    too_long = {"labels": {"page_label": "x" * 201}}
    assert (
        document_export.post(client, user_token_headers, media_file, **too_long).status_code == 422
    )
    boundary = {"labels": {"page_label": "x" * 200}}
    assert (
        document_export.post(client, user_token_headers, media_file, **boundary).status_code == 202
    )


def test_the_sync_route_points_at_the_document_route(
    client, user_token_headers, normal_user, db_session
):
    media_file = make_redactable_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/export", headers=user_token_headers, params={"format": "pdf"}
    )
    assert response.status_code == 400
    assert "POST /files/{uuid}/export/document" in response.json()["detail"]


# --- the job stream --------------------------------------------------------------------------


def test_another_users_job_stream_is_404(
    client, user_token_headers, other_user_auth_headers, normal_user, db_session, document_export
):
    from app.api.endpoints.files.subtitles import bulk_job_owned_by

    media_file = make_redactable_file(db_session, normal_user)
    job_id = document_export.post(client, user_token_headers, media_file).json()["job_id"]
    assert bulk_job_owned_by(job_id, normal_user.id), "control: the owner's own job must open"
    theirs = client.get(
        f"/api/files/bulk-export-stream?job={job_id}", headers=other_user_auth_headers
    )
    assert theirs.status_code == 404


# --- the double click ------------------------------------------------------------------------


def test_a_repeated_request_gets_the_same_job_and_renders_once(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    first = document_export.post(client, user_token_headers, media_file).json()["job_id"]
    second = document_export.post(client, user_token_headers, media_file).json()["job_id"]
    other_options = document_export.post(
        client, user_token_headers, media_file, line_numbering="none"
    ).json()["job_id"]
    assert first == second
    assert other_options != first
    assert len(document_export.dispatched) == 2  # the repeat dispatched nothing


def test_the_guard_is_released_when_the_job_ends(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    document_export.export(client, user_token_headers, media_file)
    key = document_export.dispatched[-1]["guard_key"]
    assert key.startswith(f"transcript_doc_guard:{normal_user.id}:{media_file.id}:")
    assert document_export.redis.get(key) is None
    # and so a later identical request starts a fresh job
    assert document_export.post(client, user_token_headers, media_file).status_code == 202
    assert len(document_export.dispatched) == 2


# --- run-time gates --------------------------------------------------------------------------


def test_a_job_with_no_user_refuses_and_writes_nothing(db_session, normal_user, document_export):
    media_file = make_redactable_file(db_session, normal_user)
    result = _run_task(media_file, None)
    assert result == {"status": "error", "reason": "unavailable"}
    assert document_export.uploaded == {}
    assert document_export.events[-1]["status"] == "error"


def test_a_file_quarantined_after_dispatch_writes_nothing(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    assert document_export.post(client, user_token_headers, media_file).status_code == 202
    media_file.is_quarantined = True
    db_session.commit()

    result = document_export.run_dispatched()

    assert result["status"] == "error"
    assert document_export.uploaded == {}
    # indistinguishable from a deleted file: the same fixed sentence, no mention of a takedown
    assert document_export.events[-1]["message"] == DOCUMENT_EXPORT_ERRORS["failed"]


def test_a_scan_started_after_dispatch_is_refused_and_the_refusal_is_cached(
    client, user_token_headers, normal_user, db_session, document_export
):
    enable_user_redaction(client, user_token_headers)
    media_file = make_redactable_file(db_session, normal_user)
    assert document_export.post(client, user_token_headers, media_file).status_code == 202
    media_file.redaction_status = "processing"
    db_session.commit()

    result = document_export.run_dispatched()

    assert result == {"status": "error", "reason": "redaction_pending"}
    assert document_export.uploaded == {}
    cached = document_export.redis.get(
        f"bulk_export_result:{document_export.dispatched[-1]['job_id']}"
    )
    assert cached is not None and "redaction_pending" in cached


def test_a_non_owner_never_reveals_even_when_unlocked(
    client,
    super_admin_token_headers,
    user_token_headers,
    normal_user,
    other_user,
    db_session,
    document_export,
):
    """Owner control and non-owner case on one file: only the owner's reveal is granted."""
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    owner = document_export.export(client, user_token_headers, media_file, reveal_original=True)
    assert PROFANITY in docx_text(owner), "control: the owner's reveal must work here"

    # The other user masks on their own account and asks for the original of a file they do not own.
    from app.models.prompt import UserSetting

    for key, value in (("redaction_enabled", "true"), ("redaction_categories", '["profanity"]')):
        db_session.add(UserSetting(user_id=other_user.id, setting_key=key, setting_value=value))
    db_session.commit()
    document_export.uploaded.clear()
    result = _run_task(media_file, other_user.id, reveal_original=True)
    assert result["status"] == "success", result
    text = docx_text(document_export.last_document)
    assert PROFANITY not in text
    assert CLEAN_TEXT in text


def test_the_task_never_writes_the_masked_text_back(
    client, user_token_headers, normal_user, db_session, document_export
):
    enable_user_redaction(client, user_token_headers)
    media_file = make_redactable_file(db_session, normal_user)
    document_export.export(client, user_token_headers, media_file)
    db_session.commit()  # what session_scope does when the task returns
    db_session.expire_all()
    texts = [
        s.text
        for s in db_session.query(TranscriptSegment).filter_by(media_file_id=media_file.id).all()
    ]
    assert any(PROFANITY in t for t in texts), "the export overwrote the stored transcript"


# --- hygiene ---------------------------------------------------------------------------------


def test_a_rendering_failure_logs_the_type_only_and_tells_the_user_a_fixed_sentence(
    client, user_token_headers, normal_user, db_session, document_export, monkeypatch, caplog
):
    media_file = make_redactable_file(db_session, normal_user)

    def _explode(fmt, prepared):
        raise RuntimeError(f"cannot lay out: {PROFANITY} {CLEAN_TEXT}")

    monkeypatch.setattr("app.services.transcript_document.render.render_document", _explode)
    assert document_export.post(client, user_token_headers, media_file).status_code == 202
    with caplog.at_level(logging.DEBUG):
        result = document_export.run_dispatched()

    assert result == {"status": "error", "reason": "failed"}
    assert PROFANITY not in caplog.text and CLEAN_TEXT not in caplog.text
    assert "RuntimeError" in caplog.text
    assert document_export.events[-1]["message"] == DOCUMENT_EXPORT_ERRORS["failed"]
    assert PROFANITY not in str(document_export.events)


def test_labels_are_printed_as_typed_not_formatted(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    labels = {"exported_label": "{__class__} on {date}", "page_label": "{0} {page}/{pages}"}
    text = docx_text(document_export.export(client, user_token_headers, media_file, labels=labels))
    assert "{__class__} on 20" in text
    assert "{0}" in text


def test_the_document_carries_no_author_identity(
    client, user_token_headers, normal_user, db_session, document_export
):
    import io
    import zipfile

    media_file = make_redactable_file(db_session, normal_user)
    data = document_export.export(client, user_token_headers, media_file)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        core = archive.read("docProps/core.xml").decode("utf-8")
        everything = b"".join(archive.read(n) for n in archive.namelist() if n.endswith(".xml"))
    assert "<dc:creator></dc:creator>" in core
    assert "<cp:lastModifiedBy></cp:lastModifiedBy>" in core
    assert normal_user.email.encode() not in everything
    assert b"python-docx" not in everything


def test_a_reveal_is_audited_with_the_document_surface(
    client,
    super_admin_token_headers,
    user_token_headers,
    normal_user,
    db_session,
    document_export,
    monkeypatch,
):
    events: list[dict] = []
    monkeypatch.setattr("app.auth.audit.audit_logger.log", lambda **kwargs: events.append(kwargs))
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    document_export.export(client, user_token_headers, media_file, reveal_original=True)

    revealed = [e for e in events if str(e["event_type"]) == "transcript.view_unredacted"]
    assert len(revealed) == 1, events
    assert revealed[0]["details"]["surface"] == "transcript_document_export"
    assert revealed[0]["details"]["revealed_categories"] == ["profanity"]


def test_a_non_ascii_title_downloads_under_its_own_name(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    media_file.filename = f"اجتماع-{uuid.uuid4().hex[:4]}.wav"
    db_session.commit()
    document_export.export(client, user_token_headers, media_file)
    assert document_export.presigned_names[-1] == media_file.filename.rsplit(".", 1)[0] + ".docx"


def test_the_file_row_is_untouched(
    client, user_token_headers, normal_user, db_session, document_export
):
    media_file = make_redactable_file(db_session, normal_user)
    before = (media_file.title, media_file.filename, media_file.redaction_status)
    document_export.export(client, user_token_headers, media_file)
    db_session.expire_all()
    row = db_session.query(MediaFile).filter_by(id=media_file.id).one()
    assert (row.title, row.filename, row.redaction_status) == before
