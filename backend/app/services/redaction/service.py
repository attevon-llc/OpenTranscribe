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
    ) -> tuple[list[dict], dict | None]:
        """Run cached detectors for one segment. Returns (span_dicts, toxicity_scores).

        Curated profanity + PII are cached; custom words / allowlist are read-time.
        Toxicity is a per-segment score dict (no char span). For bulk detection the
        caller sets ``run_toxicity=False`` and batches toxicity separately (faster).
        ``run_*`` flags are gated by per-language detector support.
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
            except Exception as exc:  # noqa: BLE001
                logger.warning("PII detection skipped for a segment: %s", exc)
        # Toxicity score (per-segment).
        toxicity: dict | None = None
        if run_toxicity:
            try:
                from app.services.redaction.detectors import toxicity as tox

                toxicity = tox.score_text(text, det_cfg.get("language"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Toxicity scoring skipped for a segment: %s", exc)

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
            .order_by(TranscriptSegment.start_time)
            .all()
        )

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
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Batch toxicity skipped for file %s: %s", file_id, exc)

                for idx, seg in enumerate(segments):
                    span_dicts, _ = RedactionService.detect_segment_spans(
                        str(seg.text),
                        seg.words,  # type: ignore[arg-type]
                        det_cfg,
                        run_profanity=run_profanity,
                        run_pii=run_pii,
                        run_toxicity=False,
                    )
                    if idx in llm_spans_by_idx:
                        span_dicts = span_dicts + llm_spans_by_idx[idx]
                    pii_count += sum(1 for s in span_dicts if s.get("category") == "pii")
                    seg.redactions = span_dicts or None  # type: ignore[assignment]
                    seg.toxicity = tox_scores[idx]  # type: ignore[assignment]
            media.redaction_status = C.REDACTION_STATUS_DONE  # type: ignore[assignment]
            media.redaction_model_version = C.REDACTION_MODEL_VERSION  # type: ignore[assignment]
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            media.redaction_status = C.REDACTION_STATUS_FAILED  # type: ignore[assignment]
            db.commit()
            logger.error("Redaction detection failed for file %s: %s", file_id, exc)
            return {"status": "failed", "error": str(exc)}

        ran = [d for d in ("profanity", "pii", "toxicity") if d in supported]
        if run_llm:
            ran.append("llm")
        return {
            "status": "done",
            "segments": len(segments),
            "pii_entities_found": pii_count,
            "detectors": ",".join(ran),
            "language": normalize_language(media.language),
            "skipped_detectors": sorted(skipped),
        }

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
