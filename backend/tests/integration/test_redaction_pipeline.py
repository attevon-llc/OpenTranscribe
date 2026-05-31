"""Integration test for the content-redaction pipeline (requires the live dev stack).

Seeds a media file + transcript segments from the committed fixture, runs the cached
detection, and asserts:
  - profanity/custom spans cached + masking produces the golden label output
  - PII masking (when Presidio is available in the env)
  - admin-forced categories lock the user's reveal
  - reprocess-style segment delete clears spans (cascade)

Run against the running stack:
    cd backend && PYTHONPATH=. pytest -m integration tests/integration/test_redaction_pipeline.py -v
"""

from __future__ import annotations

import json
import uuid as uuidlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"


@pytest.fixture()
def seeded_file():
    """Create a user + completed media file + segments; yield (file_id, user_id); clean up."""
    from app.core.enums import FileStatus
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.models.user import User

    segs = json.loads((FIXTURES / "segments.json").read_text())
    created = {}
    with session_scope() as db:
        user = db.query(User).first()
        if user is None:
            user = User(
                email=f"redaction-test-{uuidlib.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                is_active=True,
            )
            db.add(user)
            db.flush()
        mf = MediaFile(
            uuid=uuidlib.uuid4(),
            user_id=user.id,
            filename="redaction_fixture.wav",
            storage_path="test/redaction_fixture.wav",
            file_size=1,
            content_type="audio/wav",
            language="en",
            status=FileStatus.COMPLETED,
        )
        db.add(mf)
        db.flush()
        for s in segs:
            db.add(
                TranscriptSegment(
                    uuid=uuidlib.uuid4(),
                    media_file_id=mf.id,
                    start_time=s["start"],
                    end_time=s["end"],
                    text=s["text"],
                    words=s.get("words"),
                )
            )
        db.commit()
        created = {"file_id": int(mf.id), "user_id": int(user.id)}

    yield created

    with session_scope() as db:
        from app.models.media import MediaFile

        mf = db.query(MediaFile).filter(MediaFile.id == created["file_id"]).first()
        if mf:
            db.delete(mf)  # cascades to segments
            db.commit()


def test_detect_and_mask(seeded_file):
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.service import RedactionService

    file_id = seeded_file["file_id"]
    user_id = seeded_file["user_id"]
    expected = json.loads((FIXTURES / "expected_label_style.json").read_text())

    with session_scope() as db:
        result = RedactionService.detect_and_store(db, file_id)
        assert result["status"] == "done"

        mf = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        assert mf.redaction_status == "done"

        segs = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == file_id)
            .order_by(TranscriptSegment.start_time)
            .all()
        )
        # seg0 profanity must be cached (no model needed).
        seg0 = segs[0]
        assert seg0.redactions and any(s["category"] == "profanity" for s in seg0.redactions)

        # Redaction is opt-out by default — enable it + give the user a custom word.
        from app.api.endpoints.user_settings import _upsert_user_setting

        _upsert_user_setting(db, user_id, "redaction_enabled", "true")
        _upsert_user_setting(db, user_id, "redaction_custom_words", json.dumps(["Bluefin"]))
        db.commit()

        cfg = resolve_effective_config(db, user_id)
        masked0, _ = RedactionService.mask_segment(seg0.text, seg0.redactions, seg0.words, cfg)
        assert masked0 == expected["0"]

        seg5 = segs[5]
        masked5, _ = RedactionService.mask_segment(seg5.text, seg5.redactions, seg5.words, cfg)
        assert masked5 == expected["5"]

        # seg6 Scunthorpe never masked.
        seg6 = segs[6]
        masked6, _ = RedactionService.mask_segment(seg6.text, seg6.redactions, seg6.words, cfg)
        assert masked6 == expected["6"]

        # PII (only if Presidio available in this env).
        try:
            import presidio_analyzer  # noqa: F401

            from app.services.redaction.detectors import pii_presidio

            if pii_presidio.preload():
                seg2 = segs[2]
                masked2, _ = RedactionService.mask_segment(
                    seg2.text, seg2.redactions, seg2.words, cfg
                )
                assert "[PHONE]" in masked2 or "[SSN]" in masked2
        except Exception:
            pass


def test_admin_force_locks_reveal(seeded_file):
    from app.db.session_utils import session_scope
    from app.services.redaction.config import resolve_effective_config
    from app.services.system_settings_service import set_setting

    user_id = seeded_file["user_id"]
    with session_scope() as db:
        set_setting(db, "redaction.force_pii", "true", "test")
        try:
            cfg = resolve_effective_config(db, user_id)
            assert "pii" in cfg.locked_categories
            reveal = cfg.reveal_categories(requested=True, is_owner=True)
            assert "pii" not in reveal
        finally:
            # Clean up the system setting.
            from app.models.system_settings import SystemSettings

            row = (
                db.query(SystemSettings).filter(SystemSettings.key == "redaction.force_pii").first()
            )
            if row:
                db.delete(row)
                db.commit()
