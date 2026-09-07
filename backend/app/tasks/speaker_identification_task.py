"""
LLM-powered speaker identification task.

Provides speaker name suggestions based on conversation context, metadata,
and cross-reference analysis. Predictions are stored as suggestions for
manual user verification -- they are NOT auto-applied.

**Three phases, and the split is load-bearing.** The DB session is open only for
the two short DB-only phases (read the transcript/speakers/metadata, write the
suggestions) and is **closed** across the LLM ``identify_speakers`` call. Before
the split one ``session_scope`` wrapped the whole body, so a full
``transcript_segment`` SELECT plus a ``speaker`` SELECT sat in an open
transaction for the entire provider round trip — holding ACCESS SHARE (which
queues every ``ALTER TABLE``, i.e. any Alembic upgrade), pinning the vacuum
horizon on the largest table in the product, and consuming a pool connection.
See ``app/tasks/CLAUDE.md`` ("The session-lifetime rule").
"""

import logging
from typing import Any

from celery.exceptions import Retry
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.celery import celery_app
from app.core.constants import DEFAULT_LLM_OUTPUT_LANGUAGE
from app.core.constants import NLPPriority
from app.db.session_utils import session_scope
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment
from app.services.llm_service import LLMService
from app.services.metadata_speaker_extractor import MetadataSpeakerExtractor
from app.services.metadata_speaker_extractor import build_cross_reference_context
from app.services.metadata_speaker_extractor import cross_reference_attributes
from app.services.redaction.llm_guard import RedactionNotReadyError
from app.services.redaction.llm_guard import defer_for_redaction
from app.services.redaction.llm_guard import resolve_llm_masking
from app.utils.transcript_builders import build_full_transcript
from app.utils.transcript_builders import build_speaker_segments
from app.utils.user_settings_helpers import get_user_llm_output_language
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def _get_known_speakers(db: Session, user_id: int) -> list[dict[str, Any]]:
    """Get known speaker profiles for the user."""
    profiles = db.query(SpeakerProfile).filter(SpeakerProfile.user_id == user_id).all()
    return [
        {
            "name": profile.name,
            "description": profile.description or "No description available",
            "uuid": profile.uuid,
        }
        for profile in profiles
    ]


def _build_metadata_context(media_file) -> str:
    """Build metadata context from MediaFile for LLM speaker identification.

    Extracts useful contextual information (title, author, description, tags)
    that can help the LLM identify speakers more accurately. Also runs
    structured metadata speaker extraction to produce name hints with roles.
    """
    context_parts = []

    # Run structured metadata extraction for speaker hints
    try:
        extractor = MetadataSpeakerExtractor()
        metadata = {
            "title": media_file.title,
            "author": media_file.author,
            "description": media_file.description,
            "source_url": media_file.source_url,
            "metadata_raw": media_file.metadata_raw,
        }
        extraction_result = extractor.extract(metadata)
        structured_hints = extraction_result.to_structured_context()
        if structured_hints:
            context_parts.append(structured_hints)
            logger.info(
                f"Extracted {len(extraction_result.hints)} speaker hints from metadata "
                f"(format: {extraction_result.content_format})"
            )
    except Exception as e:
        logger.warning(f"Metadata speaker extraction failed: {e}")

    # Original flat metadata context
    if media_file.title:
        context_parts.append(f"File Title: {media_file.title}")

    if media_file.author:
        context_parts.append(f"Creator/Author: {media_file.author}")

    if media_file.description:
        desc = media_file.description
        if len(desc) > 500:
            desc = desc[:500] + "..."
        context_parts.append(f"Description: {desc}")

    if media_file.source_url:
        context_parts.append(f"Source: {media_file.source_url}")

    if media_file.metadata_raw and isinstance(media_file.metadata_raw, dict):
        metadata = media_file.metadata_raw

        tags = metadata.get("tags")
        if isinstance(tags, list) and tags:
            context_parts.append(f"Tags: {', '.join(str(t) for t in tags[:10])}")
        elif isinstance(tags, str) and tags:
            context_parts.append(f"Tags: {tags[:200]}")

        categories = metadata.get("categories")
        if isinstance(categories, list) and categories:
            context_parts.append(f"Categories: {', '.join(str(c) for c in categories[:10])}")

        uploader = metadata.get("uploader")
        if uploader:
            context_parts.append(f"Channel: {str(uploader)[:100]}")

    return "\n".join(context_parts) if context_parts else ""


def _store_metadata_hints_as_suggestions(db: Session, file_id: int, media_file) -> None:
    """Extract speaker name hints from file metadata and store them for immediate display.

    These hints appear in the UI within seconds of transcription completing,
    before the LLM speaker identification task runs.

    Args:
        db: Active database session.
        file_id: Database ID of the media file.
        media_file: MediaFile ORM object to extract metadata from.
    """
    try:
        extractor = MetadataSpeakerExtractor()
        result = extractor.extract(
            {
                "title": media_file.title,
                "author": media_file.author,
                "description": media_file.description,
                "source_url": media_file.source_url,
                "metadata_raw": media_file.metadata_raw,
            }
        )

        if not result.hints:
            return

        # Store all hints in each speaker's attribute_confidence JSONB.
        # Format: {"metadata_hints": [{"name": "Joe Rogan", "role": "host",
        #          "confidence": 0.80, "source": "title"}, ...]}
        speakers = db.query(Speaker).filter(Speaker.media_file_id == file_id).all()
        if not speakers:
            return

        hints_data = [
            {
                "name": h.name,
                "role": h.role,
                "confidence": round(h.confidence, 3),
                "source": h.source,
            }
            for h in result.hints
            if h.confidence >= 0.5  # only include reasonably confident hints
        ]

        if not hints_data:
            return

        for speaker in speakers:
            existing: dict[str, Any] = dict(speaker.attribute_confidence or {})
            existing["metadata_hints"] = hints_data
            speaker.attribute_confidence = existing  # type: ignore[assignment]
            flag_modified(speaker, "attribute_confidence")

        db.flush()
        logger.info(
            f"Stored {len(hints_data)} metadata hints for {len(speakers)} speakers "
            f"in file {file_id}"
        )

    except Exception as e:
        logger.debug(f"Metadata hint storage skipped: {e}")


def _store_alignment_results(db: Session, file_id: int, cross_refs: list[dict]) -> None:
    """Store cross-reference alignment results in speaker attribute_confidence for frontend display.

    Args:
        db: Active database session.
        file_id: Database ID of the media file.
        cross_refs: List of cross-reference dicts produced by cross_reference_attributes().
    """
    try:
        # Group by speaker_label, keep best alignment (match > mismatch > unknown)
        best_per_speaker: dict[str, dict] = {}
        for ref in cross_refs:
            label = ref.get("speaker_label", "")
            alignment = ref.get("alignment", "unknown")
            if alignment == "unknown":
                continue
            existing = best_per_speaker.get(label)
            # Prefer match over mismatch
            if existing is None or alignment == "match":
                best_per_speaker[label] = ref

        if not best_per_speaker:
            return

        speakers = db.query(Speaker).filter(Speaker.media_file_id == file_id).all()
        speaker_map: dict[str, Speaker] = {str(s.name): s for s in speakers}

        for label, ref in best_per_speaker.items():
            spk = speaker_map.get(label)
            if not spk:
                continue
            existing_conf: dict[str, Any] = dict(spk.attribute_confidence or {})
            existing_conf["alignment"] = ref["alignment"]
            existing_conf["alignment_hint"] = ref["hint_name"]
            spk.attribute_confidence = existing_conf  # type: ignore[assignment]
            flag_modified(spk, "attribute_confidence")

        db.flush()
        logger.info(
            f"Stored alignment results for {len(best_per_speaker)} speakers in file {file_id}"
        )
    except Exception as e:
        logger.debug(f"Alignment storage skipped: {e}")


def _create_llm_service(user_id: int | None) -> LLMService:
    """Create LLM service based on user settings or system defaults."""
    if user_id:
        llm_service = LLMService.create_from_user_settings(user_id)
    else:
        llm_service = LLMService.create_from_system_settings()

    if not llm_service:
        raise Exception("Could not create LLM service for speaker identification")
    return llm_service


def _run_llm_identification(
    llm_service: LLMService,
    full_transcript: str,
    speaker_segments: list[dict[str, Any]],
    known_speakers: list[dict[str, Any]],
    output_language: str = "en",
    metadata_context: str = "",
) -> dict[str, Any]:
    """Run LLM speaker identification and return predictions."""
    try:
        if hasattr(llm_service, "identify_speakers"):
            return llm_service.identify_speakers(
                transcript=full_transcript,
                speaker_segments=speaker_segments,
                known_speakers=known_speakers,
                output_language=output_language,
                metadata_context=metadata_context,
            )
        logger.warning("Speaker identification not implemented - skipping")
        return {"speaker_predictions": [], "error": "Feature not implemented"}
    finally:
        llm_service.close()


def _store_speaker_predictions(file_id: int, predictions: dict[str, Any]) -> None:
    """Store speaker predictions as suggestions in speaker records.

    Opens its **own** short session: the caller has just returned from the LLM
    provider and deliberately holds none.

    A ≥0.75-confidence prediction here moves the CANONICAL label
    (``canonical_speaker_label``) with no ``display_name`` write at all — this
    was one of five writers with zero chunk-plane dispatch (issue #605): a clean
    ingest indexes chunks under the raw diarizer label, this task's suggestion
    then lands after (``enrich_and_dispatch`` dispatches indexing before speaker
    ID), and nothing reconciled the two. Every prediction is recorded on a
    tracker keyed by this ONE file, then flushed once after the explicit commit
    below — predictions in the same call commonly target different speakers
    with different names, which is exactly the case ``SpeakerRenameTracker``
    groups by new name at flush time.
    """
    from app.services.speaker_rename_tracker import SpeakerRenameTracker
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    tracker = SpeakerRenameTracker()
    with session_scope() as db:
        for prediction in predictions.get("speaker_predictions", []):
            speaker_label = prediction.get("speaker_label")
            predicted_name = prediction.get("predicted_name")
            confidence = prediction.get("confidence", 0.0)

            if confidence < 0.5:
                continue

            speaker = (
                db.query(Speaker)
                .filter(Speaker.media_file_id == file_id, Speaker.name == speaker_label)
                .first()
            )

            if speaker:
                before = canonical_speaker_label_for_row(speaker)
                speaker.suggested_name = predicted_name
                speaker.confidence = confidence
                speaker.suggestion_source = "llm_analysis"  # type: ignore[assignment]
                tracker.record(file_id, before, canonical_speaker_label_for_row(speaker))

        # Commit explicitly (rather than relying on session_scope's exit-time
        # commit) so the tracker can flush — dispatch after commit, never
        # before — while this session is still open to resolve the file UUID.
        db.commit()
        tracker.flush(db)


def _apply_cross_reference_context(
    db: Session,
    file_id: int,
    media_file,
    speaker_segments: list[dict[str, Any]],
    metadata_context: str,
) -> str:
    """Fold gender/metadata cross-references into the prompt context.

    Pure DB + regex work, so it belongs in the read phase; it also *writes*
    alignment results back to ``speaker.attribute_confidence`` for the UI.
    """
    try:
        speakers_with_attrs = (
            db.query(Speaker)
            .filter(
                Speaker.media_file_id == file_id,
                Speaker.predicted_gender.isnot(None),
            )
            .all()
        )
        if not speakers_with_attrs:
            return metadata_context

        speaker_attrs = {
            str(s.name): {
                "predicted_gender": s.predicted_gender,
                "predicted_age_range": s.predicted_age_range,
            }
            for s in speakers_with_attrs
        }
        extractor = MetadataSpeakerExtractor()
        extraction = extractor.extract(
            {
                "title": media_file.title,
                "author": media_file.author,
                "description": media_file.description,
                "metadata_raw": media_file.metadata_raw,
            }
        )
        cross_refs = cross_reference_attributes(extraction.hints, speaker_attrs, speaker_segments)
        xref_context = build_cross_reference_context(cross_refs)
        if xref_context:
            metadata_context = f"{metadata_context}\n\n{xref_context}"
            logger.info(f"Added {len(cross_refs)} cross-references to context")
        # Store alignment results in speaker attribute_confidence for frontend display
        if cross_refs:
            _store_alignment_results(db, file_id, cross_refs)
    except Exception as e:
        logger.debug(f"Cross-reference enrichment skipped: {e}")

    return metadata_context


def _load_identification_inputs(file_uuid: str, task_id: str) -> dict[str, Any]:
    """Phase 1 — read everything the LLM phase needs, then release the session.

    Returns **plain data only**; no ORM instance escapes, because one that did
    would lazy-load during the provider call and silently reopen a transaction.
    ``{"early_result": ...}`` means the task is done (no speakers to identify).

    Raises:
        ValueError: File or transcript missing, or the redaction policy could not
            be resolved (fail closed).
        RedactionNotReadyError: Detection has not cached spans yet.
    """
    from sqlalchemy.orm import joinedload

    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            raise ValueError(f"Media file with UUID {file_uuid} not found")

        file_id = int(media_file.id)
        user_id = int(media_file.user_id)

        create_task_record(db, task_id, user_id, file_id, "speaker_identification")
        update_task_status(db, task_id, "in_progress", progress=0.1)

        # Store metadata hints immediately so they appear in the UI before the LLM call
        _store_metadata_hints_as_suggestions(db, file_id, media_file)
        try:
            db.commit()
        except Exception as e:
            logger.debug(f"Metadata hints commit skipped: {e}")
            db.rollback()

        # ``joinedload`` rather than a lazy load per row: the transcript builders
        # read ``segment.speaker`` for every segment.
        transcript_segments = (
            db.query(TranscriptSegment)
            .options(joinedload(TranscriptSegment.speaker))
            .filter(TranscriptSegment.media_file_id == file_id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

        if not transcript_segments:
            raise ValueError(f"No transcript segments found for file {file_id}")

        speaker_count = (
            db.query(Speaker.id).filter(Speaker.media_file_id == file_id).count()  # noqa: N806
        )
        if not speaker_count:
            logger.info(f"No speakers found for file {file_id}, skipping LLM identification")
            update_task_status(db, task_id, "completed", progress=1.0, completed=True)
            return {
                "early_result": {"status": "skipped", "message": "No speakers to identify"},
            }

        # Redact PII/profanity before the transcript leaves for the LLM
        # provider, exactly as the summarization task does. This path used to
        # pass no config at all, so `redact_before_llm` (and the admin
        # `force_redact_before_llm` floor) had no effect on speaker
        # identification. Defer if spans aren't cached; fail closed if the
        # policy cannot be resolved at all.
        try:
            redaction_cfg = resolve_llm_masking(db, media_file)
        except RedactionNotReadyError:
            # ``defer_for_redaction`` needs the bound task and must not run with
            # this session open — the caller handles it.
            raise
        except Exception as _redact_err:
            logger.exception(
                "Could not resolve the redaction policy; aborting speaker identification "
                "rather than sending a possibly unredacted transcript to an external provider"
            )
            raise ValueError(
                "Redaction policy unavailable; speaker identification aborted to avoid "
                "sending unredacted content to an external provider"
            ) from _redact_err

        # Masking + formatting is pure CPU over rows already fetched, but it
        # reads ``segment.speaker``/``segment.words``, so it stays in the scope.
        # What leaves is a string and a list of dicts.
        full_transcript = build_full_transcript(transcript_segments, redaction_cfg)
        speaker_segments = build_speaker_segments(transcript_segments, redaction_cfg=redaction_cfg)
        known_speakers = _get_known_speakers(db, user_id)
        metadata_context = _build_metadata_context(media_file)
        if metadata_context:
            logger.info(f"Built metadata context for speaker ID ({len(metadata_context)} chars)")

        metadata_context = _apply_cross_reference_context(
            db, file_id, media_file, speaker_segments, metadata_context
        )

        output_language = (
            get_user_llm_output_language(db, user_id) if user_id else DEFAULT_LLM_OUTPUT_LANGUAGE
        )

        update_task_status(db, task_id, "in_progress", progress=0.5)

    return {
        "file_id": file_id,
        "user_id": user_id,
        "full_transcript": full_transcript,
        "speaker_segments": speaker_segments,
        "known_speakers": known_speakers,
        "metadata_context": metadata_context,
        "output_language": output_language,
    }


def _mark_identification_failed(task_id: str, error_msg: str) -> None:
    """Record the failure in its own short session."""
    from app.utils.task_utils import update_task_status

    try:
        with session_scope() as db:
            update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)
    except Exception as cleanup_err:  # noqa: BLE001
        logger.error(f"Could not record speaker-identification failure: {cleanup_err}")


@celery_app.task(bind=True, name="ai.identify_speakers", priority=NLPPriority.USER_TRIGGERED)
def identify_speakers_llm_task(self, file_uuid: str):
    """
    Use LLM to provide speaker identification suggestions

    This task provides suggestions to help users identify speakers manually.
    The predictions are NOT automatically applied to the transcript.

    Args:
        file_uuid: UUID of the MediaFile
    """
    from app.utils.task_utils import update_task_status

    task_id = self.request.id
    file_id = None

    try:
        # Phase 1 — read (DB session open, Postgres only).
        inputs = _load_identification_inputs(file_uuid, task_id)
        if "early_result" in inputs:
            return inputs["early_result"]

        file_id = inputs["file_id"]
        user_id = inputs["user_id"]

        # Phase 2 — the LLM call. NO DB session is held here.
        predictions = _generate_predictions(
            file_id,
            user_id,
            inputs["full_transcript"],
            inputs["speaker_segments"],
            inputs["known_speakers"],
            inputs["metadata_context"],
            inputs["output_language"],
        )

        # Phase 3 — write (short sessions, Postgres only).
        _store_speaker_predictions(file_id, predictions)
        with session_scope() as db:
            update_task_status(db, task_id, "completed", progress=1.0, completed=True)

        # Notify enrichment tracker
        send_ws_event(
            user_id,
            "enrichment_task_complete",
            {"file_id": file_uuid, "task": "speaker_identification"},
        )

        return {
            "status": "success",
            "file_id": file_id,
            "predictions_count": len(predictions.get("speaker_predictions", [])),
            "overall_confidence": predictions.get("overall_confidence", "unknown"),
        }

    except Retry:
        # Celery signals deferral with an exception that subclasses Exception, so
        # the broad handler below would "handle" it and mark the task failed.
        raise
    except RedactionNotReadyError as not_ready:
        try:
            defer_for_redaction(self, not_ready)
        except Retry:
            raise
        except Exception as fatal:
            logger.error(f"Speaker identification cannot proceed for {file_uuid}: {fatal}")
            _mark_identification_failed(task_id, str(fatal))
            return {"status": "error", "message": str(fatal)}
        raise  # unreachable — defer_for_redaction always raises
    except Exception as e:
        logger.error(f"Error in speaker identification task for file {file_id}: {str(e)}")
        _mark_identification_failed(task_id, str(e))
        return {"status": "error", "message": str(e)}


def _generate_predictions(
    file_id: int,
    user_id: int | None,
    full_transcript: str,
    speaker_segments: list[dict[str, Any]],
    known_speakers: list[dict[str, Any]],
    metadata_context: str = "",
    output_language: str = DEFAULT_LLM_OUTPUT_LANGUAGE,
) -> dict[str, Any]:
    """Phase 2 — run the LLM. **No DB session is held here.**

    ``LLMService.create_from_user_settings`` opens and closes its own short
    session internally, which is the shape the rule asks for: a callee that needs
    the DB opens its own scope instead of borrowing one that then spans the
    provider round trip.
    """
    try:
        logger.info(f"Starting LLM speaker identification for file {file_id}")
        logger.info(f"Using LLM output language: {output_language}")

        llm_service = _create_llm_service(user_id)
        predictions = _run_llm_identification(
            llm_service,
            full_transcript,
            speaker_segments,
            known_speakers,
            output_language,
            metadata_context,
        )

        logger.info(
            f"Generated {len(predictions.get('speaker_predictions', []))} speaker predictions"
        )
        return predictions

    except Exception as e:
        logger.error(f"LLM speaker identification failed: {type(e).__name__}: {e}")
        logger.error("Full traceback:", exc_info=True)
        return {"speaker_predictions": [], "error": str(e)}
