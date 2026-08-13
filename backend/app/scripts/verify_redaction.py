"""End-to-end verification of content redaction against the live stack + real models.

Seeds a media file + transcript segments (synthetic, fake PII), runs the real detection
(Presidio + spaCy/GLiNER + toxicity), applies read-time masking, prints a report, and
cleans up. Run inside a container that has the models + DB access::

    docker exec opentranscribe-celery-redaction python -m app.scripts.verify_redaction

Exit code 0 if the core expectations pass, 1 otherwise. Safe to re-run (self-cleaning).
"""

from __future__ import annotations

import sys
import uuid as uuidlib

_SEGMENTS = [
    "This is fucking ridiculous, I can't believe it.",
    "My name is John Smith and my email is john.smith@example.com.",
    "Call me at 555-123-4567, my SSN is 123-45-6789.",
    "Put it on card 4111 1111 1111 1111, expires 09/27.",
    "You are an idiot and I hate you people.",
    "The project codename is Bluefin, keep it quiet.",
    "I mailed the contract to Scunthorpe last week.",
]


def main() -> int:  # noqa: C901
    from app.core.enums import FileStatus
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.models.user import User
    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.service import RedactionService

    file_id = None
    user_id = None
    try:
        with session_scope() as db:
            user = db.query(User).first()
            if not user:
                print("FAIL: no user in DB to own the test file")
                return 1
            user_id = int(user.id)
            mf = MediaFile(
                uuid=uuidlib.uuid4(),
                user_id=user.id,
                filename="verify_redaction.wav",
                storage_path="test/verify_redaction.wav",
                file_size=1,
                content_type="audio/wav",
                language="en",
                status=FileStatus.COMPLETED,
            )
            db.add(mf)
            db.flush()
            file_id = int(mf.id)
            t = 0.0
            for text in _SEGMENTS:
                db.add(
                    TranscriptSegment(
                        uuid=uuidlib.uuid4(),
                        media_file_id=mf.id,
                        start_time=t,
                        end_time=t + 3,
                        text=text,
                    )
                )
                t += 3
            db.commit()

        # Run the REAL detection (whatever models are loaded in this container).
        with session_scope() as db:
            result = RedactionService.detect_and_store(db, file_id)
        print(f"detect_and_store -> {result}\n")

        # Give the owner a custom word so seg5 masks, then render masked text.
        with session_scope() as db:
            import json

            from app.api.endpoints.user_settings import _upsert_user_setting

            # Redaction is opt-out by default — enable it for this verification user.
            _upsert_user_setting(db, user_id, "redaction_enabled", "true")
            _upsert_user_setting(db, user_id, "redaction_custom_words", json.dumps(["Bluefin"]))
            db.commit()
            cfg = resolve_effective_config(db, user_id)
            segs = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.media_file_id == file_id)
                .order_by(
                    TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id
                )
                .all()
            )
            print("Masked output (read-time):")
            print("-" * 70)
            checks = {
                "profanity": False,
                "pii_name": False,
                "pii_email": False,
                "pii_phone": False,
                "pii_ssn": False,
                "pii_credit_card": False,
                "custom_or_name_masked": False,
                "profanity_word_boundary_safe": True,
                "toxicity": False,
            }
            for i, s in enumerate(segs):
                masked, _ = RedactionService.mask_segment(s.text, s.redactions, s.words, cfg)
                tox = RedactionService.is_segment_toxic(s.toxicity, cfg)
                print(f"  [{i}] {masked}" + ("   <toxic>" if tox else ""))
                if i == 0:
                    checks["profanity"] = "[PROFANITY]" in masked
                if i == 1:
                    checks["pii_name"] = "[NAME]" in masked
                    checks["pii_email"] = "[EMAIL]" in masked
                if i == 2:
                    checks["pii_phone"] = "[PHONE]" in masked
                    checks["pii_ssn"] = "[SSN]" in masked
                if i == 3:
                    checks["pii_credit_card"] = "[CREDIT_CARD]" in masked
                if i == 4:
                    checks["toxicity"] = tox
                if i == 5:
                    # "Bluefin" is masked either as a custom word or (legitimately) as a NAME.
                    checks["custom_or_name_masked"] = "Bluefin" not in masked
                if i == 6:
                    # The profanity wordlist must NOT match the "cunt" substring in "Scunthorpe"
                    # (it may still be masked as a LOCATION/NAME by NER — that's correct PII).
                    checks["profanity_word_boundary_safe"] = "[PROFANITY]" not in masked
            print("-" * 70)
            print("\nChecks:")
            for k, v in checks.items():
                print(f"  {'PASS' if v else 'FAIL'}  {k}")

        # Everything above should pass with the models loaded in this container.
        all_ok = all(checks.values())
        print(f"\nOVERALL: {'PASS ✅' if all_ok else 'FAIL ❌'}")
        return 0 if all_ok else 1
    finally:
        if file_id is not None:
            with session_scope() as db:
                mf = db.query(MediaFile).filter(MediaFile.id == file_id).first()
                if mf:
                    db.delete(mf)
                    db.commit()


if __name__ == "__main__":
    sys.exit(main())
