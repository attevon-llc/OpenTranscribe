"""Toxicity detector — segment-level scoring (no word spans).

Default: ``unitary/toxic-bert`` (English, multi-label). For non-English segments the
multilingual toxic XLM-RoBERTa model is used. Produces a score dict per segment stored on
``transcript_segment.toxicity``; the read-time layer flags/blurs a segment when ``toxic``
exceeds the user's threshold. Loaded on CPU and moved GPU↔CPU per scan by free VRAM.
"""

from __future__ import annotations

import logging

from app.core import constants as C  # noqa: N812

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
    """Return a toxicity score dict for one segment (None if model unavailable)."""
    if not text or not text.strip():
        return None
    model_name = _model_for_language(language)
    pipe = _get_pipe(model_name)
    if pipe is None:
        return None
    from app.services.redaction.device import inference_guard
    from app.services.redaction.device import resolve_device

    try:
        with inference_guard():
            _place_on_device(pipe, resolve_device())
            raw = pipe(text[:2000])
        return _normalize_scores(raw, model_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Toxicity scoring failed: %s", exc)
        return None


def score_texts(
    texts: list[str], language: str | None = None, batch_size: int = 32
) -> list[dict | None]:
    """Batch-score many segments in one model pass (fast path for long transcripts).

    Returns a list aligned 1:1 with ``texts`` (None for blanks / on failure).
    """
    model_name = _model_for_language(language)
    pipe = _get_pipe(model_name)
    if pipe is None:
        return [None] * len(texts)

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
    try:
        raw_all = pipe(inputs, batch_size=batch_size)
        for slot, raw in zip(idx_map, raw_all):
            # Per-input result is a list[{label,score}] when top_k=None.
            results[slot] = _normalize_scores([raw] if isinstance(raw, list) else raw, model_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Batch toxicity scoring failed: %s", exc)
    return results
