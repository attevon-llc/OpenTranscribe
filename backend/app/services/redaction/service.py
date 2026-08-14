"""RedactionService — detection (cached, once per transcript) + read-time masking.

Detection runs ALL expensive detectors unconditionally and caches the spans; enabling
/disabling, categories, style, custom words and allowlist are all applied cheaply at
read time, so toggling never triggers a recompute.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import detection_config_for_all
from app.services.redaction.config import detector_language_support
from app.services.redaction.config import normalize_language
from app.services.redaction.detectors import DetectorUnavailableError
from app.services.redaction.detectors import wordlist
from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import apply_redactions

logger = logging.getLogger(__name__)


class RedactionService:
    """Stateless orchestrator. Detector models are process-wide singletons."""

    # ------------------------------------------------------------------ detection
    @staticmethod
    def detect_segment_spans(
        text: str,
        words: list[dict] | None,
        det_cfg: dict,
        *,
        run_profanity: bool = True,
        run_pii: bool = True,
        run_toxicity: bool = True,
        failures: list[str] | None = None,
        unavailable: list[str] | None = None,
    ) -> tuple[list[dict], dict | None]:
        """Run cached detectors for one segment. Returns (span_dicts, toxicity_scores).

        Curated profanity + PII are cached; custom words / allowlist are read-time.
        Toxicity is a per-segment score dict (no char span). For bulk detection the
        caller sets ``run_toxicity=False`` and batches toxicity separately (faster).
        ``run_*`` flags are gated by per-language detector support.

        Args:
            failures: Optional sink. A detector that could not produce a trustworthy
                result appends its name here so the caller can tell "found nothing"
                apart from "could not look". Detection is cached once and never
                re-run, so a swallowed failure would otherwise be indistinguishable
                from a clean result forever (issue #324). **Every masker reads this
                one** — an unavailable detector is as unsafe as a broken one when
                the next step is posting the text to a provider.
            unavailable: Optional second sink, a SUBSET of ``failures``: detectors
                that could not run at all (dependency/model absent). Only
                ``detect_and_store`` cares, because re-running will not install a
                dependency — see :class:`DetectorUnavailableError`.
        """
        spans: list[RedactionSpan] = []
        # Profanity (curated, user-independent → safe to cache).
        if run_profanity:
            spans.extend(wordlist.find_profanity_spans(text, words))
        # PII (Presidio + GLiNER) — heavy, lazy import.
        if run_pii:
            try:
                from app.services.redaction.detectors import pii_presidio

                spans.extend(pii_presidio.detect_pii(text, words, det_cfg))
            except DetectorUnavailableError as exc:
                logger.warning("PII detector unavailable for a segment: %s", exc)
                if failures is not None:
                    failures.append("pii")
                if unavailable is not None:
                    unavailable.append("pii")
            except Exception as exc:  # noqa: BLE001
                logger.warning("PII detection skipped for a segment: %s", exc)
                if failures is not None:
                    failures.append("pii")
        # Toxicity score (per-segment). Recorded in the same two sinks as PII, but
        # note it can never make a masker withhold text: ``blocking_detector_failures``
        # maps ``toxicity`` to no category, because this detector emits a score and
        # never a span. What its absence costs is the toxic FLAG, which is a coverage
        # fact — hence the sinks — not an unmasked-text fact.
        toxicity: dict | None = None
        if run_toxicity:
            try:
                from app.services.redaction.detectors import toxicity as tox

                toxicity = tox.score_text(text, det_cfg.get("language"))
            except DetectorUnavailableError as exc:
                logger.warning("Toxicity detector unavailable for a segment: %s", exc)
                if failures is not None:
                    failures.append("toxicity")
                if unavailable is not None:
                    unavailable.append("toxicity")
            except Exception as exc:  # noqa: BLE001
                # Was ``logger.debug`` and nothing else, so a toxicity-only fault
                # reached neither sink and marked nothing — invisible by construction.
                logger.warning("Toxicity scoring failed for a segment: %s", exc)
                if failures is not None:
                    failures.append("toxicity")

        return [s.model_dump() for s in spans], toxicity

    @staticmethod
    def detect_and_store(db: Session, file_id: int) -> dict:
        """Detect + cache redaction spans for every segment of a file. Idempotent."""
        media = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if media is None:
            return {"status": "skipped", "reason": "file_not_found"}

        media.redaction_status = C.REDACTION_STATUS_PROCESSING  # type: ignore[assignment]
        db.commit()

        det_cfg = detection_config_for_all()
        det_cfg["language"] = media.language
        # GLiNER (enhanced name detection) is an admin toggle; default from the env constant.
        try:
            from app.services.system_settings_service import get_setting_bool

            det_cfg["pii_use_gliner"] = get_setting_bool(
                db, "redaction.pii_use_gliner", C.REDACTION_PII_USE_GLINER
            )
        except Exception:  # noqa: BLE001
            det_cfg["pii_use_gliner"] = C.REDACTION_PII_USE_GLINER
        # Language gating: skip detectors that don't support this transcript's language.
        supported, skipped = detector_language_support(media.language)
        run_profanity = "profanity" in supported
        run_pii = "pii" in supported
        run_toxicity = "toxicity" in supported
        # Run LLM detector only if the owner enabled it (it needs their provider).
        run_llm = RedactionService._owner_wants_llm(db, int(media.user_id))
        if skipped:
            logger.info(
                "Redaction: skipping detectors %s for file %s (language=%s not supported)",
                sorted(skipped),
                file_id,
                media.language,
            )

        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == file_id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

        # Detectors that RAISED, as opposed to ones deliberately skipped for the
        # transcript's language. Detection is cached once and never re-run, so a
        # swallowed failure would be cached as "nothing found" permanently — a
        # transcript full of PII would look clean forever (issue #324). Anything
        # in here means the cached result is not trustworthy.
        detector_failures: list[str] = []
        # The subset of the above that could not run AT ALL. Subtracted before the
        # FAILED decision below: FAILED means "re-run me", and re-running does not
        # install a missing dependency. See DetectorUnavailableError for why that
        # distinction is load-bearing rather than cosmetic.
        detector_unavailable: list[str] = []

        llm_spans_by_idx: dict[int, list] = {}
        if run_llm:
            try:
                from app.services.redaction.detectors import llm

                seg_dicts = [{"text": s.text, "words": s.words} for s in segments]
                llm_spans_by_idx = {
                    idx: [sp.model_dump() for sp in spans]
                    for idx, spans in llm.detect_with_llm(seg_dicts, int(media.user_id)).items()
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM redaction detection failed for file %s: %s", file_id, exc)
                detector_failures.append("llm")

        # Serialize GPU inference (no-op on CPU) so concurrent files never stack model
        # activations on one GPU — peak VRAM stays bounded to a single inference.
        from app.services.redaction.device import inference_guard

        pii_count = 0
        try:
            with inference_guard():
                # Batch toxicity scoring across all segments in one model pass (much
                # faster than per-segment). Skipped for unsupported languages.
                tox_scores: list[dict | None] = [None] * len(segments)
                if run_toxicity:
                    try:
                        from app.services.redaction.detectors import toxicity as tox

                        tox_scores = tox.score_texts(
                            [str(s.text or "") for s in segments], media.language
                        )
                    except DetectorUnavailableError as exc:
                        # No weights on this box. Reported, never FAILED — re-running
                        # downloads nothing, and `failed` is a permanent refusal in
                        # `llm_guard`. `toxicity` is a DEFAULT category, so getting
                        # this arm wrong breaks every redaction-enabled user at once.
                        logger.warning(
                            "Toxicity detector unavailable for file %s: %s", file_id, exc
                        )
                        detector_failures.append("toxicity")
                        detector_unavailable.append("toxicity")
                    except Exception as exc:  # noqa: BLE001
                        # It ran and threw. Worth re-running, so this one IS a failure.
                        logger.warning("Batch toxicity failed for file %s: %s", file_id, exc)
                        detector_failures.append("toxicity")

                for idx, seg in enumerate(segments):
                    span_dicts, _ = RedactionService.detect_segment_spans(
                        str(seg.text),
                        seg.words,  # type: ignore[arg-type]
                        det_cfg,
                        run_profanity=run_profanity,
                        run_pii=run_pii,
                        run_toxicity=False,
                        failures=detector_failures,
                        unavailable=detector_unavailable,
                    )
                    if idx in llm_spans_by_idx:
                        span_dicts = span_dicts + llm_spans_by_idx[idx]
                    pii_count += sum(1 for s in span_dicts if s.get("category") == "pii")
                    seg.redactions = span_dicts or None  # type: ignore[assignment]
                    seg.toxicity = tox_scores[idx]  # type: ignore[assignment]
            # An UNAVAILABLE detector is reported as skipped, not failed — FAILED is
            # not an inert label, and llm_guard turns it into a permanent refusal
            # (see DetectorUnavailableError). The maskers are unaffected either way:
            # they read `failures`, which records unavailability too.
            unavailable = sorted(set(detector_unavailable))
            if unavailable:
                for name in unavailable:
                    skipped[name] = "unavailable"
                logger.warning(
                    "Redaction detection for file %s ran without detectors %s "
                    "(unavailable on this deployment); their categories were NOT "
                    "examined and the cached spans do not cover them.",
                    file_id,
                    unavailable,
                )

            hard_failures = sorted(set(detector_failures) - set(unavailable))
            if hard_failures:
                # Do NOT cache a degraded pass as DONE. Detection runs once and is
                # never re-run, so marking this complete would permanently record
                # "no PII found" for a transcript nobody actually finished scanning
                # (issue #324). FAILED keeps the file eligible for re-detection.
                #
                # The spans that DID succeed are committed — they are real findings
                # and dropping them would be strictly worse — but the status makes
                # clear the result is partial.
                failed = hard_failures
                media.redaction_status = C.REDACTION_STATUS_FAILED  # type: ignore[assignment]
                db.commit()
                logger.error(
                    "Redaction detection for file %s completed with failed detectors %s; "
                    "marking FAILED so it is re-run rather than caching an incomplete "
                    "result as clean. Partial spans found so far were kept.",
                    file_id,
                    failed,
                )
                return {"status": "failed", "reason": "detector_failure", "detectors": failed}

            # WHICH detectors this scan's spans reflect (v392). Written in the same
            # commit as DONE, because the whole point is that the two must never be
            # read apart: `done` says the scan finished and this says what it looked
            # at. `skipped` already carries both reasons a detector did not run — the
            # transcript's language, and unavailability recorded a few lines above.
            ran = [
                d for d in ("profanity", "pii", "toxicity") if d in supported and d not in skipped
            ]
            if run_llm:
                ran.append("llm")
            media.redaction_status = C.REDACTION_STATUS_DONE  # type: ignore[assignment]
            media.redaction_model_version = C.REDACTION_MODEL_VERSION  # type: ignore[assignment]
            media.redaction_coverage = ran  # type: ignore[assignment]
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            media.redaction_status = C.REDACTION_STATUS_FAILED  # type: ignore[assignment]
            db.commit()
            logger.error("Redaction detection failed for file %s: %s", file_id, exc)
            return {"status": "failed", "error": str(exc)}

        return {
            "status": "done",
            "segments": len(segments),
            "pii_entities_found": pii_count,
            "detectors": ",".join(ran),
            "language": normalize_language(media.language),
            "skipped_detectors": sorted(skipped),
        }

    @staticmethod
    def redetect_edited_segment(
        db: Session, media_file: MediaFile, segment: TranscriptSegment
    ) -> str:
        """Re-detect ONE edited segment, or record that it could not be examined.

        A segment edit invalidates that segment's cached spans — they address
        offsets in text that no longer exists — so the API process re-runs
        detection inline. It has no Presidio or toxicity weights loaded
        (``celery-redaction`` is the only process that preloads them), so this is
        also the path most likely to meet a detector that cannot run.

        **The failure mode this exists to prevent is a WRITE.**
        ``detect_segment_spans`` swallows a detector exception and returns the
        spans it did collect, so "found nothing" and "could not look" are the
        same value (issue #324). Persisting that value cached the outage: every
        later read took the cached-span path, found nothing to mask, and sent the
        segment on. Unlike the request-scoped fixes in ``chat/redactor.py`` and
        the detector layer, the leak then outlived its cause — the detector never
        had to fail again.

        So on a **blocking** failure the file is marked ``pending`` and a full
        re-scan is queued instead. ``pending`` is not a new concept: it is the
        state ``_lazy_dispatch_redaction`` already uses, and every status-aware
        reader honours it (the transcript read withholds, chat's cached path
        refuses and falls back to inline fail-closed masking, ``llm_guard``
        defers). ``failed`` would be wrong — ``llm_guard`` turns it into a
        NON-retryable refusal, permanently breaking summarization for the file —
        and refusing the edit outright would be worse still: a user fixing a
        typo should not be blocked by a detector outage.

        What *is* persisted is the spans the detectors that DID run found. They
        are real findings and they were detected against the NEW text, so their
        offsets address the text they are stored beside; the previous spans are
        simply gone, because there is no meaningful way to realign them. Same
        disposition ``detect_and_store`` takes on a partial pass: keep the
        findings, let the status say the result is incomplete.

        Blocking is decided against the **file owner's** effective policy — the
        subject ``llm_guard.resolve_llm_masking`` uses, because the content is
        theirs. Resolving the *editor's* config would let an admin whose own
        redaction is off cache an unexamined segment as clean in someone else's
        transcript. And it is narrow on purpose
        (:func:`~app.services.redaction.config.blocking_detector_failures`): only
        a detector feeding a category that policy actually masks may withhold, so
        a CPU-only deployment with no Presidio that never enabled ``pii`` is
        untouched.

        Args:
            db: Database session. Committed only when the file is marked stale.
            media_file: The edited segment's file (the policy subject).
            segment: The segment whose ``text`` just changed. Its ``redactions``
                and ``toxicity`` are updated in place.

        Returns:
            ``"done"`` when the cached spans can be trusted, ``"stale"`` when the
            file was marked for re-detection instead.
        """
        from app.services.redaction.config import blocking_detector_failures
        from app.services.redaction.config import resolve_effective_config

        failures: list[str] = []
        try:
            det_cfg = detection_config_for_all()
            det_cfg["language"] = media_file.language
            span_dicts, toxicity = RedactionService.detect_segment_spans(
                str(segment.text),
                segment.words,  # type: ignore[arg-type]
                det_cfg,
                failures=failures,
            )
        except Exception as exc:  # noqa: BLE001
            # Not just one detector — the whole re-detection. Nothing was
            # examined, and the segment's cached spans still describe the text it
            # had before the edit.
            logger.warning("Re-detection after a segment edit raised: %s", exc)
            return RedactionService._mark_redaction_stale(db, media_file, ["*"])

        blocking: list[str] = []
        if failures:
            try:
                cfg = resolve_effective_config(db, int(media_file.user_id))
                blocking = sorted(blocking_detector_failures(failures, cfg.enabled_categories))
            except Exception:  # noqa: BLE001
                # Fail CLOSED: an unresolvable policy is not an absent policy. If
                # we cannot tell whether the owner masks these categories, we
                # cannot tell that caching this result is safe.
                logger.exception(
                    "Could not resolve the owner's redaction policy after a segment edit; "
                    "treating every failed detector as blocking"
                )
                blocking = sorted(set(failures))

        segment.redactions = span_dicts or None  # type: ignore[assignment]
        segment.toxicity = toxicity  # type: ignore[assignment]
        if blocking:
            return RedactionService._mark_redaction_stale(db, media_file, blocking)
        return "done"

    @staticmethod
    def _mark_redaction_stale(db: Session, media_file: MediaFile, detectors: list[str]) -> str:
        """Record that a file's cached spans no longer cover its text, and re-scan it.

        Args:
            db: Database session (committed here so the queued task cannot read
                the pre-edit rows).
            media_file: File whose cached spans are no longer trustworthy.
            detectors: What could not be trusted, for the operator-facing log.

        Returns:
            Always ``"stale"``. Never raises: a detector outage must not turn a
            transcript edit into a 500.
        """
        status = getattr(media_file, "redaction_status", None)
        already_queued = status in (C.REDACTION_STATUS_PENDING, C.REDACTION_STATUS_PROCESSING)
        media_file.redaction_status = C.REDACTION_STATUS_PENDING  # type: ignore[assignment]
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "Could not mark file %s stale after an unexaminable segment edit", media_file.id
            )
            return "stale"

        logger.warning(
            "A segment edit on file %s could not be examined by detectors %s. The file's "
            "cached spans are marked stale (pending) and a full re-detection was queued, "
            "rather than caching an unexamined segment as clean.",
            media_file.id,
            detectors,
        )
        if already_queued:
            # A scan is already coming; a second one would just duplicate the CPU
            # work for someone editing several segments in a row.
            return "stale"
        try:
            from app.tasks.redaction_task import redaction_detect_task

            redaction_detect_task.delay(file_id=int(media_file.id), user_id=int(media_file.user_id))
        except Exception:  # noqa: BLE001
            # The status stands either way — the file stays withheld rather than
            # silently reverting to "scanned and clean".
            logger.exception("Could not queue re-detection for file %s", media_file.id)
        return "stale"

    @staticmethod
    def _owner_wants_llm(db: Session, user_id: int) -> bool:
        try:
            from app.services.redaction.config import resolve_effective_config

            return "llm" in resolve_effective_config(db, user_id).detectors
        except Exception:  # noqa: BLE001
            return False

    # --------------------------------------------------------------- read-time mask
    @staticmethod
    def mask_segment(
        text: str,
        cached_spans: list[dict] | None,
        words: list[dict] | None,
        cfg: EffectiveRedactionConfig,
        reveal_categories: set[str] | None = None,
    ) -> tuple[str, list[dict]]:
        """Apply masking to one segment's text. Returns (masked_text, applied_span_dicts)."""
        if not cfg.enabled or not text:
            return text, []

        allowset = {a.strip().lower() for a in cfg.allowlist if a.strip()}
        spans: list = []

        for raw in cached_spans or []:
            cat = raw.get("category")
            # Filter cached PII by the entity types the user/admin actually wants masked.
            if cat == "pii" and raw.get("entity_type") not in cfg.pii_entities:
                continue
            # Allowlist (read-time) — never mask explicitly allowed words.
            covered = text[raw.get("char_start", 0) : raw.get("char_end", 0)].strip().lower()
            if covered and covered in allowset:
                continue
            spans.append(raw)

        # Custom words are per-user → matched at read time.
        if "custom" in cfg.enabled_categories and cfg.custom_words:
            spans.extend(
                s.model_dump()
                for s in wordlist.find_custom_spans(text, cfg.custom_words, words, cfg.allowlist)
            )

        masked, applied = apply_redactions(
            text,
            spans,
            style=cfg.style,
            enabled_categories=cfg.enabled_categories,
            reveal_categories=reveal_categories or set(),
        )
        return masked, [s.model_dump() for s in applied]

    @staticmethod
    def is_segment_toxic(toxicity: dict | None, cfg: EffectiveRedactionConfig) -> bool:
        """Whether a segment should be flagged toxic given the user's threshold."""
        if not toxicity or "toxicity" not in cfg.enabled_categories:
            return False
        return float(toxicity.get("toxic", 0.0)) >= cfg.toxicity_threshold
