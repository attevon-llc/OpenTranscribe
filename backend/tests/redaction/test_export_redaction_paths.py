"""The three export surfaces must apply the reader's redaction policy (issue #85).

``GET /files/{uuid}/subtitles`` has resolved a policy since ``0eecd839``. The other two
export paths are Celery tasks and resolved **nothing** — ``build_subtitle_archive`` (the
bulk-export ZIP) and ``video_processing_service`` (burned-in subtitles) called the
subtitle generators with no config, which ``_redact_segments_inplace`` reads as
"redaction is disabled". They therefore exported the raw transcript for every user,
**including under the admin ``redaction.force_export_redacted`` floor** — a control the
UI presents as covering exports.

Every assertion here reads the BYTES the user would receive (the SRT inside the ZIP, the
SRT written for ffmpeg to burn in), never an internal flag. Against ``HEAD~`` these fail
with the profanity and the email address printed in the failure message.

Two controls keep the fix from being "mask everything always":
:func:`test_a_deployment_with_redaction_disabled_exports_verbatim` and the
owner-enabled/reader-disabled half of :func:`test_the_subject_is_the_reader_not_the_owner`.
"""

from __future__ import annotations

import contextlib
import io
import uuid
import zipfile
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.prompt import UserSetting
from app.services.redaction.config import resolve_effective_config
from app.services.redaction.export_policy import ExportRedactionNotReadyError
from app.services.redaction.export_policy import export_masking_is_pending
from app.services.redaction.export_policy import export_policy_fingerprint
from app.services.subtitle_service import SubtitleService
from app.services.system_settings_service import set_setting

# The admin floor lives in SystemSettings, whose key namespace is shared state.
pytestmark = pytest.mark.xdist_group("redaction_system_settings")

TEXT = "Damn it, email me at john.smith@example.com."
PROFANITY = "Damn"
PII = "john.smith@example.com"


def _cached_spans() -> list[dict]:
    """The spans a finished detection scan would have cached for :data:`TEXT`.

    Constructed rather than detected: this suite is about which policy the export
    applies, and every export path masks from the CACHED spans. The detectors
    themselves are covered by ``test_presidio.py`` / ``test_wordlist.py``.
    """
    return [
        {
            "char_start": TEXT.index(PROFANITY),
            "char_end": TEXT.index(PROFANITY) + len(PROFANITY),
            "category": "profanity",
            "entity_type": "PROFANITY",
            "detector": "wordlist",
        },
        {
            "char_start": TEXT.index(PII),
            "char_end": TEXT.index(PII) + len(PII),
            "category": "pii",
            # The app's own entity vocabulary, not Presidio's: `pii_presidio` maps
            # EMAIL_ADDRESS -> EMAIL, and `mask_segment` filters cached spans against
            # `cfg.pii_entities`, which is spelled in the app's names.
            "entity_type": "EMAIL",
            "detector": "presidio",
        },
    ]


def _make_file(db_session, owner, *, redaction_status: str = "done") -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename=f"export85-{uuid.uuid4().hex[:8]}.mp4",
        storage_path="media/test/export85.mp4",
        content_type="video/mp4",
        file_size=1024,
        user_id=owner.id,
        status="completed",
        redaction_status=redaction_status,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    db_session.add(
        TranscriptSegment(
            uuid=str(uuid.uuid4()),
            media_file_id=media_file.id,
            start_time=0.0,
            end_time=3.0,
            text=TEXT,
            redactions=_cached_spans(),
        )
    )
    db_session.commit()
    return media_file


def _enable_redaction(db_session, user, categories: str = '["profanity", "pii"]') -> None:
    for key, value in (("redaction_enabled", "true"), ("redaction_categories", categories)):
        db_session.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.commit()


def _archive_for(db_session, reader, media_file, fmt: str = "srt") -> tuple[str, int, int]:
    """Build the ZIP exactly as ``prepare_bulk_subtitles_task`` does, and read it."""
    cfg = resolve_effective_config(db_session, reader.id)
    zip_bytes, exported, skipped = SubtitleService.build_subtitle_archive(
        db_session,
        [(int(media_file.id), "export85")],
        fmt,
        True,
        cfg,
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        content = zf.read(names[0]).decode("utf-8") if names else ""
    return content, exported, skipped


@pytest.fixture
def owned_file(db_session, sample_user):
    return _make_file(db_session, sample_user)


class TestBulkExportArchive:
    def test_the_archive_masks_with_the_requesting_users_policy(
        self, db_session, sample_user, owned_file
    ):
        _enable_redaction(db_session, sample_user)

        content, exported, _ = _archive_for(db_session, sample_user, owned_file)

        assert exported == 1
        assert PROFANITY not in content, f"profanity exported unmasked:\n{content}"
        assert PII not in content, f"PII exported unmasked:\n{content}"
        assert "[PROFANITY]" in content and "[EMAIL]" in content, content

    def test_the_archive_obeys_the_admin_force_floor(self, db_session, sample_user, owned_file):
        """The headline claim: the admin mandated censored exports for everyone.

        The user here has redaction OFF. ``force_export_redacted`` is what the admin UI
        describes as covering exports, and this path ignored it entirely.
        """
        set_setting(db_session, "redaction.force_profanity", "true")
        set_setting(db_session, "redaction.force_pii", "true")
        set_setting(db_session, "redaction.force_export_redacted", "true")
        db_session.commit()

        content, exported, _ = _archive_for(db_session, sample_user, owned_file)

        assert exported == 1
        assert PROFANITY not in content, f"the admin floor was ignored:\n{content}"
        assert PII not in content, f"the admin floor was ignored:\n{content}"

    def test_a_deployment_with_redaction_disabled_exports_verbatim(
        self, db_session, sample_user, owned_file
    ):
        """CONTROL. Redaction is opt-out; nothing here masks, so nothing may change.

        Without this the fix could be "mask everything always" and still pass.
        """
        content, exported, skipped = _archive_for(db_session, sample_user, owned_file)

        assert (exported, skipped) == (1, 0)
        # Compared per token, not against TEXT: SRT wraps at 42 characters, so the
        # sentence is split across cue lines even when nothing is masked.
        assert PROFANITY in content and PII in content, content

    def test_the_subject_is_the_reader_not_the_owner(
        self, db_session, sample_user, other_user, owned_file
    ):
        """Both directions of the subject decision, on one shared file.

        The single-file export resolves the REQUESTING user, so the batch must too:
        two buttons applying different policies to the same file makes the weaker one
        the real policy for anyone who knows which to press. Resolving the OWNER here
        would hand this reader an unmasked ZIP of a file the transcript page and the
        single-file download both mask for them.
        """
        _enable_redaction(db_session, other_user)  # the READER masks; the OWNER does not

        reader_content, _, _ = _archive_for(db_session, other_user, owned_file)
        owner_content, _, _ = _archive_for(db_session, sample_user, owned_file)

        assert PII not in reader_content, (
            f"the reader's own policy was ignored — owner's policy applied:\n{reader_content}"
        )
        # CONTROL for the same decision: the owner masks nothing, and imposing the
        # reader's policy on them (or anyone's) would be a different bug.
        assert PROFANITY in owner_content and PII in owner_content, owner_content

    def test_a_file_whose_scan_is_unfinished_is_skipped_not_exported_raw(
        self, db_session, sample_user
    ):
        """Masking reads CACHED spans, so a mid-scan file would export raw silently.

        The single-file endpoint answers 409; a batch cannot fail 99 files for one, so
        it skips. What it must never do is write the unmasked transcript into the ZIP.
        """
        _enable_redaction(db_session, sample_user)
        unscanned = _make_file(db_session, sample_user, redaction_status="processing")

        content, exported, skipped = _archive_for(db_session, sample_user, unscanned)

        assert (exported, skipped) == (0, 1)
        assert content == ""

    def test_the_export_never_writes_the_masked_text_back(
        self, db_session, sample_user, owned_file
    ):
        """Masking mutates the loaded ORM objects — and the workers COMMIT.

        ``build_subtitle_archive`` runs inside ``session_scope``, which commits on
        exit. Before the expunge in ``_redact_segments_inplace`` this test flushes
        ``[PROFANITY] it, email me at [EMAIL].`` into ``transcript_segment.text``:
        turning masking on would have destroyed the original transcript, which the
        entire read-time-masking design exists to preserve.
        """
        _enable_redaction(db_session, sample_user)

        _archive_for(db_session, sample_user, owned_file)
        db_session.commit()  # exactly what session_scope does when the task returns
        db_session.expire_all()

        stored = (
            db_session.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == owned_file.id)
            .one()
        )
        assert stored.text == TEXT, "the export overwrote the stored transcript"

    def test_a_failed_scan_still_exports(self, db_session, sample_user):
        """CONTROL for the gate above: ``failed`` must not trap the user forever.

        It means the scan could not run, not that it is coming — the same disposition
        every other status-aware reader takes.
        """
        _enable_redaction(db_session, sample_user)
        failed = _make_file(db_session, sample_user, redaction_status="failed")

        content, exported, skipped = _archive_for(db_session, sample_user, failed)

        assert (exported, skipped) == (1, 0)
        assert content.strip()


class TestBulkExportWorker:
    """The task itself — the wiring, not just the helper it calls."""

    def test_the_worker_masks_using_the_requesting_user(
        self, db_session, sample_user, owned_file, monkeypatch
    ):
        from app.tasks import media_download as mdl

        _enable_redaction(db_session, sample_user)

        @contextlib.contextmanager
        def _scope():
            yield db_session

        uploaded: dict[str, bytes] = {}

        class _FakeService:
            cache_bucket = "processed"

            def __init__(self, minio):
                self.minio_service = self

            def upload_bytes(self, bucket, key, data, content_type):
                uploaded["zip"] = data

        # `MinIOService()` itself is left real: its __init__ only binds the module-level
        # client singleton, so nothing here reaches the network through it.
        monkeypatch.setattr(mdl, "session_scope", _scope)
        monkeypatch.setattr(mdl, "VideoProcessingService", _FakeService)
        monkeypatch.setattr(
            "app.services.download_events.publish_bulk_event", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "app.services.minio_service.get_presigned_download_url",
            lambda *a, **kw: "http://minio.invalid/x.zip",
        )
        monkeypatch.setattr("app.core.redis.get_redis", lambda: _NoopRedis())

        result = mdl.prepare_bulk_subtitles_task.run(
            file_specs=[[int(owned_file.id), "export85"]],
            subtitle_format="srt",
            include_speakers=True,
            job_id="job85",
            user_id=int(sample_user.id),
        )

        assert result["status"] == "success", result
        with zipfile.ZipFile(io.BytesIO(uploaded["zip"])) as zf:
            content = zf.read(zf.namelist()[0]).decode("utf-8")
        assert PROFANITY not in content, f"the worker exported unmasked bytes:\n{content}"
        assert PII not in content, f"the worker exported unmasked bytes:\n{content}"

    def test_a_job_with_no_requesting_user_refuses_to_build(self, monkeypatch):
        """Version skew (a message queued before #85) must fail closed, not export raw."""
        from app.tasks import media_download as mdl

        events: list[dict] = []
        monkeypatch.setattr(
            mdl,
            "session_scope",
            lambda: pytest.fail("the archive must not be built without a subject"),
        )
        monkeypatch.setattr(
            "app.services.download_events.publish_bulk_event",
            lambda job_id, **kw: events.append(kw),
        )

        result = mdl.prepare_bulk_subtitles_task.run(
            file_specs=[[1, "x"]],
            subtitle_format="srt",
            include_speakers=True,
            job_id="job85-nouser",
            user_id=None,
        )

        assert result["status"] == "error"
        assert events and events[0]["status"] == "error"


class _NoopRedis:
    def setex(self, *args, **kwargs):
        return True


class TestBurnedInSubtitles:
    """The least recoverable surface: once masked text is pixels, it cannot be fixed."""

    def _service(self):
        from app.services import video_processing_service as vps

        return vps, vps.VideoProcessingService.__new__(vps.VideoProcessingService)

    def _render(self, db_session, monkeypatch, reader, media_file) -> str:
        vps, service = self._service()

        @contextlib.contextmanager
        def _scope():
            yield db_session

        monkeypatch.setattr(vps, "session_scope", _scope)
        written: dict[str, str] = {}

        class _Path:
            def write_text(self, content, encoding=None):
                written["srt"] = content

        cfg = service._resolve_export_policy(int(media_file.id), int(reader.id))
        service._generate_subtitle_file(int(media_file.id), _Path(), True, cfg)
        return written["srt"]

    def test_the_burned_in_srt_is_masked(self, db_session, sample_user, owned_file, monkeypatch):
        _enable_redaction(db_session, sample_user)

        srt = self._render(db_session, monkeypatch, sample_user, owned_file)

        assert PROFANITY not in srt, f"profanity burned into the video:\n{srt}"
        assert PII not in srt, f"PII burned into the video:\n{srt}"

    def test_an_unmasked_deployment_burns_the_transcript_verbatim(
        self, db_session, sample_user, owned_file, monkeypatch
    ):
        """CONTROL: no policy masks, so the render must be unchanged."""
        srt = self._render(db_session, monkeypatch, sample_user, owned_file)

        assert PROFANITY in srt and PII in srt, srt

    def test_a_render_is_refused_while_the_scan_is_unfinished(
        self, db_session, sample_user, monkeypatch
    ):
        vps, service = self._service()
        _enable_redaction(db_session, sample_user)
        unscanned = _make_file(db_session, sample_user, redaction_status="pending")

        @contextlib.contextmanager
        def _scope():
            yield db_session

        monkeypatch.setattr(vps, "session_scope", _scope)

        with pytest.raises(ExportRedactionNotReadyError):
            service._resolve_export_policy(int(unscanned.id), int(sample_user.id))


class TestExportPolicyFingerprint:
    """The cache key for a burned-in render has to name the policy it was made under."""

    def test_a_policy_that_masks_nothing_has_an_empty_fingerprint(self, db_session, sample_user):
        cfg = resolve_effective_config(db_session, sample_user.id)

        assert cfg.enabled is False
        assert export_policy_fingerprint(cfg) == "", (
            "a deployment with redaction off must keep byte-identical cache keys"
        )

    def test_policies_that_mask_differently_get_different_fingerprints(
        self, db_session, sample_user, other_user
    ):
        _enable_redaction(db_session, sample_user, categories='["profanity"]')
        _enable_redaction(db_session, other_user, categories='["profanity", "pii"]')

        one = export_policy_fingerprint(resolve_effective_config(db_session, sample_user.id))
        two = export_policy_fingerprint(resolve_effective_config(db_session, other_user.id))

        assert one and two
        assert one != two, "two policies that mask different text shared a cached render"

    def test_the_fingerprint_ignores_ordering(self, db_session, sample_user, other_user):
        _enable_redaction(db_session, sample_user, categories='["profanity", "pii"]')
        _enable_redaction(db_session, other_user, categories='["pii", "profanity"]')

        assert export_policy_fingerprint(
            resolve_effective_config(db_session, sample_user.id)
        ) == export_policy_fingerprint(resolve_effective_config(db_session, other_user.id)), (
            "the same policy written in a different order must reuse the cached render"
        )


class TestExportMaskingIsPending:
    @pytest.mark.parametrize(
        "status,expected",
        [("done", False), ("failed", False), ("pending", True), ("processing", True), (None, True)],
    )
    def test_the_status_rule(self, db_session, sample_user, status, expected):
        _enable_redaction(db_session, sample_user)
        cfg = resolve_effective_config(db_session, sample_user.id)

        assert export_masking_is_pending(cfg, status) is expected

    def test_nothing_is_withheld_when_the_policy_masks_nothing(self, db_session, sample_user):
        """CONTROL: the gate must not withhold exports on deployments with no redaction."""
        cfg = resolve_effective_config(db_session, sample_user.id)

        assert export_masking_is_pending(cfg, None) is False
        assert export_masking_is_pending(None, "processing") is False


class TestSubtitleRevealAudit:
    """``?redact=false`` on an export writes the same compliance event as the page."""

    def test_revealing_an_export_is_audited(self, client, auth_headers, db_session, sample_user):
        _enable_redaction(db_session, sample_user)
        media_file = _make_file(db_session, sample_user)

        calls: list[dict] = []
        with patch("app.auth.audit.audit_logger") as fake_audit:
            fake_audit.log.side_effect = lambda **kw: calls.append(kw)
            response = client.get(
                f"/api/files/{media_file.uuid}/subtitles?redact=false",
                headers=auth_headers,
            )

        assert response.status_code == 200
        assert PII in response.text, "the owner asked for the original and is entitled to it"
        assert len(calls) == 1, f"the reveal wrote no audit event: {calls}"
        assert calls[0]["event_type"] == "transcript.view_unredacted"
        assert calls[0]["details"]["file_uuid"] == str(media_file.uuid)
        assert calls[0]["details"]["surface"] == "subtitle_export"
        assert sorted(calls[0]["details"]["revealed_categories"]) == ["pii", "profanity"]

    def test_a_masked_export_writes_no_event(self, client, auth_headers, db_session, sample_user):
        """CONTROL: only a REVEAL is auditable; an ordinary download is not."""
        _enable_redaction(db_session, sample_user)
        media_file = _make_file(db_session, sample_user)

        calls: list[dict] = []
        with patch("app.auth.audit.audit_logger") as fake_audit:
            fake_audit.log.side_effect = lambda **kw: calls.append(kw)
            response = client.get(f"/api/files/{media_file.uuid}/subtitles", headers=auth_headers)

        assert response.status_code == 200
        assert PII not in response.text
        assert calls == []

    def test_an_admin_forced_category_is_never_revealed_or_audited_as_one(
        self, client, auth_headers, db_session, sample_user
    ):
        """The floor outranks ``?redact=false``; there is nothing to audit revealing."""
        _enable_redaction(db_session, sample_user)
        set_setting(db_session, "redaction.force_pii", "true")
        set_setting(db_session, "redaction.force_export_redacted", "true")
        db_session.commit()
        media_file = _make_file(db_session, sample_user)

        calls: list[dict] = []
        with patch("app.auth.audit.audit_logger") as fake_audit:
            fake_audit.log.side_effect = lambda **kw: calls.append(kw)
            response = client.get(
                f"/api/files/{media_file.uuid}/subtitles?redact=false",
                headers=auth_headers,
            )

        assert response.status_code == 200
        assert PII not in response.text, f"the forced category was revealed:\n{response.text}"
        assert calls == []
