"""Toxicity detector — segment-level scoring (no word spans).

Default: ``unitary/toxic-bert`` (English, multi-label). For non-English segments the
multilingual toxic XLM-RoBERTa model is used. Produces a score dict per segment stored on
``transcript_segment.toxicity``; the read-time layer flags/blurs a segment when ``toxic``
exceeds the user's threshold. Loaded on CPU and moved GPU↔CPU per scan by free VRAM.

**A model that could not be loaded raises :class:`DetectorUnavailableError`; an
inference that threw propagates.** Neither degrades to ``None`` any more. ``None`` is
the legitimate answer for *blank text*, and it was also the answer for "the weights are
not on this box" and for "the pipeline blew up" — three states, one value, and
``detect_segment_spans`` logged the difference at ``logger.debug`` and dropped it. A
toxicity-only fault was therefore completely invisible: nothing marked the file, nothing
reported a skip, and the cached ``toxicity`` column read as "scored, not toxic".

The two dispositions differ downstream exactly as they do for PII: unavailability is a
reported skip (re-running installs no weights) and a thrown inference is a hard failure
worth re-running. What they deliberately do NOT do is withhold text — see
``config._DETECTOR_CATEGORIES``, where ``toxicity`` maps to no category, because this
detector emits no spans and so can leave nothing unmasked.
"""

from __future__ import annotations

import logging

from app.core import constants as C  # noqa: N812
from app.services.redaction.detectors import DetectorUnavailableError

logger = logging.getLogger(__name__)

_pipes: dict[str, object] = {}
_load_failed: set[str] = set()


def _model_for_language(language: str | None) -> str:
    if language and language.lower() not in ("en", "en-us", "english", "auto", ""):
        return C.REDACTION_TOXICITY_MODEL_MULTI
    return C.REDACTION_TOXICITY_MODEL_EN


def _get_pipe(model_name: str):
    """Get the toxicity pipeline (loaded on CPU; moved to the live device per scan)."""
    if model_name in _pipes:
        return _pipes[model_name]
    if model_name in _load_failed:
        return None
    try:
        from transformers import pipeline

        # Always load on CPU; _place_on_device() moves it to GPU at scan time when there's
        # free VRAM (and back to CPU when the GPU is busy) — no restart needed.
        pipe = pipeline(
            "text-classification",
            model=model_name,
            top_k=None,  # return all label scores
            device=-1,
            truncation=True,
            max_length=512,
        )
        _pipes[model_name] = pipe
        logger.info(
            "Toxicity model loaded: %s (device=cpu, auto-moves to GPU when free)", model_name
        )
        return pipe
    except Exception as exc:  # noqa: BLE001
        logger.error("Toxicity model %s failed to load: %s", model_name, exc)
        _load_failed.add(model_name)
        return None


def _place_on_device(pipe, target: str) -> None:
    """Move the pipeline to ``target`` ('cpu' or 'cuda:N') if it isn't already there.

    Cheap no-op when already on the right device. Called per scan so the model tracks
    live GPU availability without a restart.
    """
    try:
        import torch

        want = torch.device(target)
        cur = getattr(pipe, "device", None)
        if cur is not None and cur.type == want.type and (cur.index or 0) == (want.index or 0):
            return
        pipe.model.to(want)
        pipe.device = want
        logger.info("Toxicity model moved to %s", target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not move toxicity model to %s: %s", target, exc)


def preload() -> bool:
    """Eagerly load the default English toxicity model (worker_ready hook)."""
    return _get_pipe(C.REDACTION_TOXICITY_MODEL_EN) is not None


def _normalize_scores(raw, model_name: str) -> dict:
    """Convert a transformers output into a {label: score} dict + a 'toxic' summary."""
    scores: dict[str, float] = {}
    # raw is typically list[list[{label,score}]] or list[{label,score}] for one input
    items = raw[0] if raw and isinstance(raw[0], list) else raw
    for entry in items or []:
        label = str(entry.get("label", "")).lower()
        scores[label] = float(entry.get("score", 0.0))
    # Derive a single "toxic" probability across common label schemes.
    toxic = (
        scores.get("toxic")
        or scores.get("toxicity")
        or scores.get("label_1")
        or scores.get("hate")
        or 0.0
    )
    scores["toxic"] = float(toxic)
    scores["model"] = model_name  # type: ignore[assignment]
    return scores


def score_text(text: str, language: str | None = None) -> dict | None:
    """Return a toxicity score dict for one segment.

    Args:
        text: The segment text to score.
        language: Transcript language, selecting the English or multilingual model.

    Returns:
        The score dict, or ``None`` for blank text — the one remaining meaning of
        ``None``, and now unambiguous.

    Raises:
        DetectorUnavailableError: The model could not be loaded, so nothing was
            scored. Previously ``None``, which is also what a clean blank segment
            returns and what a crashed inference returned.
        Exception: Whatever the pipeline raises. It ran and failed, which is worth
            re-running; unavailability is not.
    """
    if not text or not text.strip():
        return None
    model_name = _model_for_language(language)
    pipe = _get_pipe(model_name)
    if pipe is None:
        raise DetectorUnavailableError(
            f"Toxicity model {model_name} could not be loaded (missing transformers "
            "install or model weights); this text was never scored for toxicity"
        )
    from app.services.redaction.device import inference_guard
    from app.services.redaction.device import resolve_device

    with inference_guard():
        _place_on_device(pipe, resolve_device())
        raw = pipe(text[:2000])
    return _normalize_scores(raw, model_name)


def score_texts(
    texts: list[str], language: str | None = None, batch_size: int = 32
) -> list[dict | None]:
    """Batch-score many segments in one model pass (fast path for long transcripts).

    Args:
        texts: Segment texts, in order.
        language: Transcript language, selecting the English or multilingual model.
        batch_size: Pipeline batch size.

    Returns:
        A list aligned 1:1 with ``texts``, ``None`` for blanks.

    Raises:
        DetectorUnavailableError: The model could not be loaded. It used to return
            ``[None] * len(texts)`` — the same value a transcript of blank segments
            produces — so ``detect_and_store`` wrote a full column of NULL toxicity
            scores and reported the detector as having run.
        Exception: Whatever the pipeline raises. The batch used to be caught here and
            reported as a list of ``None``, so a scan whose toxicity pass crashed
            completed as ``done`` with nothing to show for it.
    """
    model_name = _model_for_language(language)
    pipe = _get_pipe(model_name)
    if pipe is None:
        raise DetectorUnavailableError(
            f"Toxicity model {model_name} could not be loaded (missing transformers "
            "install or model weights); these segments were never scored for toxicity"
        )

    # Place on the live device. NOTE: callers (detect_and_store) already hold the GPU
    # inference guard, so we do NOT re-acquire it here (the lock is non-reentrant).
    from app.services.redaction.device import resolve_device

    _place_on_device(pipe, resolve_device())

    # Map non-blank texts to their indices; blanks stay None.
    idx_map = [i for i, t in enumerate(texts) if t and t.strip()]
    inputs = [texts[i][:2000] for i in idx_map]
    results: list[dict | None] = [None] * len(texts)
    if not inputs:
        return results
    raw_all = pipe(inputs, batch_size=batch_size)
    for slot, raw in zip(idx_map, raw_all, strict=True):
        # Per-input result is a list[{label,score}] when top_k=None.
        results[slot] = _normalize_scores([raw] if isinstance(raw, list) else raw, model_name)
    return results
