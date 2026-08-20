"""PII detector — Microsoft Presidio (regex recognizers) + optional GLiNER (names).

Heavy: imports presidio/spaCy/gliner lazily and builds a process-wide singleton
``AnalyzerEngine``. Loaded only by the celery-redaction worker.

**A missing dependency raises :class:`DetectorUnavailableError`; it does NOT
degrade to "no spans".** "Presidio is not installed" and "this segment is clean"
are the same return value otherwise, and the caller that has to tell them apart
is the one about to post the text to an LLM provider. Degrading quietly is still
the *disposition* the pipeline chooses — ``detect_and_store`` records the
detector as skipped and completes — but that is now a decision made where the
policy lives, not a fact hidden where the model loads.
"""

from __future__ import annotations

import logging
import threading

from app.core import constants as C  # noqa: N812
from app.services.redaction.detectors import DetectorUnavailableError
from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import build_word_offsets
from app.services.redaction.spans import map_char_span_to_words

logger = logging.getLogger(__name__)

# Presidio entity type → our entity_type label.
_ENTITY_MAP = {
    "PERSON": "NAME",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "US_SSN": "SSN",
    "CREDIT_CARD": "CREDIT_CARD",
    "IBAN_CODE": "IBAN",
    "IP_ADDRESS": "IP_ADDRESS",
    "US_BANK_NUMBER": "BANK_ACCOUNT",
    "LOCATION": "LOCATION",
    "GPE": "LOCATION",
    "ADDRESS": "ADDRESS",
    "ORGANIZATION": "ORGANIZATION",
    "ORG": "ORGANIZATION",
    "NRP": "NAME",
}

# Max chars per analyze() call — chunk longer segment text to respect model limits.
_MAX_CHARS = 2000

_analyzer = None  # singleton
_analyzer_gliner: bool | None = None  # which GLiNER mode the cached analyzer was built with
_load_failed = False

# Serializes the BUILD, not the reads. Building takes ~7-10 s, so without this two
# callers arriving inside that window each built their OWN engine: measured 2
# distinct engines, ~11.6 s apiece (vs 10.1 s solo — they contend for CPU), and
# ~2x the ~500 MB peak. Neither caller benefited from the other's work. That is the
# API process's normal case, not an edge case, because the warm-up below runs
# concurrently with inbound requests by design.
_analyzer_lock = threading.Lock()


def _build_analyzer(use_gliner: bool):
    """Build the Presidio AnalyzerEngine with en_core_web_sm + optional GLiNER."""
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    nlp_conf = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_conf)
    nlp_engine = provider.create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])

    # Presidio's built-in US_SSN recognizer scores the dashed format below typical
    # thresholds without strong context. SSN is high-value PII and xxx-xx-xxxx is
    # unambiguous, so register a high-confidence pattern recognizer for it.
    try:
        from presidio_analyzer import Pattern
        from presidio_analyzer import PatternRecognizer

        ssn = PatternRecognizer(
            supported_entity="US_SSN",
            patterns=[
                Pattern(name="ssn-dashed", regex=r"\b\d{3}-\d{2}-\d{4}\b", score=0.85),
                Pattern(name="ssn-spaced", regex=r"\b\d{3} \d{2} \d{4}\b", score=0.6),
            ],
            context=["ssn", "social security", "social security number"],
        )
        analyzer.registry.add_recognizer(ssn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register SSN pattern recognizer: %s", exc)

    # Names/orgs/locations come from spaCy NER by default (fast: ~10-20 ms/segment).
    # GLiNER is a higher-accuracy but ~7x slower enhancement (~130 ms/segment CPU) —
    # admin/ops opt-in, and GPU-capable via REDACTION_DEVICE.
    if not use_gliner:
        logger.info("PII names via spaCy NER (fast default; GLiNER disabled)")
        return analyzer
    try:
        from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

        from app.services.redaction.device import resolve_device

        # Resolver verifies CUDA + free VRAM, else falls back to CPU.
        map_location = resolve_device()
        gliner = GLiNERRecognizer(
            model_name=C.REDACTION_PII_GLINER_MODEL,
            entity_mapping={
                "person": "PERSON",
                "name": "PERSON",
                "organization": "ORGANIZATION",
                "address": "ADDRESS",
                "location": "LOCATION",
            },
            flat_ner=False,
            multi_label=True,
            map_location=map_location,
        )
        analyzer.registry.add_recognizer(gliner)
        logger.info(
            "GLiNER PII recognizer registered (%s, device=%s)",
            C.REDACTION_PII_GLINER_MODEL,
            map_location,
        )
    except Exception as exc:  # noqa: BLE001 — optional enhancement
        logger.warning("GLiNER recognizer unavailable, using spaCy NER for names: %s", exc)

    return analyzer


def _cached(use_gliner: bool):
    """The usable cached analyzer, or None if one must be built.

    Split out only so the fast path and the post-lock re-check cannot drift apart.
    """
    if _analyzer is not None and _analyzer_gliner == use_gliner:
        return _analyzer
    if _load_failed and _analyzer is not None:
        return _analyzer  # keep what we have if a rebuild previously failed
    return None


def _get_analyzer(use_gliner: bool):
    """Return the analyzer, rebuilding it if the GLiNER mode changed (admin toggle).

    Double-checked locking: the cache hit stays lock-free (measured at ~2 us, and
    it is on every masked segment), while a **build** is serialized so a caller
    arriving mid-build waits for that build instead of starting a second one.
    Waiting is strictly the better outcome — it is the same engine, sooner, for
    less CPU — and the caller was going to block for a cold build either way.
    """
    global _analyzer, _analyzer_gliner, _load_failed
    cached = _cached(use_gliner)
    if cached is not None:
        return cached
    with _analyzer_lock:
        # Re-check: whoever held the lock may have built exactly what we need.
        cached = _cached(use_gliner)
        if cached is not None:
            return cached
        try:
            _analyzer = _build_analyzer(use_gliner)
            _analyzer_gliner = use_gliner
            _load_failed = False
        except Exception as exc:  # noqa: BLE001
            logger.error("Presidio analyzer failed to load — PII detection disabled: %s", exc)
            _load_failed = True
            return _analyzer  # may be None
        return _analyzer


def preload() -> bool:
    """Eagerly build the analyzer (called from the worker_ready hook). Returns success."""
    return _get_analyzer(C.REDACTION_PII_USE_GLINER) is not None


def detect_pii(text: str, words: list[dict] | None, cfg: dict) -> list[RedactionSpan]:
    """Return PII spans (category='pii') for one segment's text.

    Args:
        text: The segment text to examine.
        words: Word timings, used to map char spans back to word indices.
        cfg: Detection config (``pii_entities``, ``pii_confidence``, ``pii_use_gliner``).

    Returns:
        The PII spans found. An empty list means the analyzer ran and found
        nothing — never that it could not run.

    Raises:
        DetectorUnavailableError: The analyzer could not be built, so nothing was
            examined.
        Exception: Whatever ``analyzer.analyze`` raises. A chunk that raised is a
            chunk nobody looked at, and the segment's result is therefore partial.
    """
    if not text or not text.strip():
        return []
    use_gliner = bool(cfg.get("pii_use_gliner", C.REDACTION_PII_USE_GLINER))
    analyzer = _get_analyzer(use_gliner)
    if analyzer is None:
        raise DetectorUnavailableError(
            "Presidio analyzer could not be built (missing dependency, spaCy model "
            "or weights); this text was never examined for PII"
        )

    keep_entities = set(cfg.get("pii_entities", C.REDACTION_PII_ENTITIES))
    min_score = float(cfg.get("pii_confidence", C.DEFAULT_REDACTION_PII_CONFIDENCE))
    word_offsets = build_word_offsets(text, words)

    spans: list[RedactionSpan] = []
    # Chunk long text; offset results back to absolute positions.
    for base in range(0, len(text), _MAX_CHARS):
        chunk = text[base : base + _MAX_CHARS]
        # No `except: continue` here. Skipping a chunk that raised left the rest
        # of the segment's PII unfound and returned the result as though it were
        # complete — the same "could not look reads as clean" shape as an absent
        # analyzer, one frame lower. Propagating instead lets
        # ``detect_segment_spans`` record it in ``failures``, which is what every
        # fail-closed masker reads. Unlike an unbuildable analyzer this IS worth
        # re-running, so it stays an ordinary exception.
        results = analyzer.analyze(text=chunk, language="en")
        raw = [(r.entity_type, r.start, r.end, float(r.score)) for r in results]
        spans.extend(_spans_from_raw(raw, base, word_offsets, keep_entities, min_score))
    return spans


def _spans_from_raw(
    raw: list[tuple],
    base: int,
    word_offsets,
    keep_entities: set,
    min_score: float,
) -> list[RedactionSpan]:
    """Turn one chunk's analyzer output into spans, offset back to absolute positions.

    Shared by the per-segment and batched paths so the two cannot drift: the batched path
    differs only in *where* analyze() ran, never in how its results are filtered or mapped.
    ``raw`` items are ``(entity_type, start, end, score)`` — plain tuples, because
    RecognizerResult does not survive a process boundary.
    """
    spans: list[RedactionSpan] = []
    for entity_raw, r_start, r_end, score in raw:
        entity_type = _ENTITY_MAP.get(entity_raw)
        if entity_type is None or entity_type not in keep_entities:
            continue
        if score < min_score:
            continue
        cs, ce = base + r_start, base + r_end
        ws, we = map_char_span_to_words(word_offsets, cs, ce)
        spans.append(
            RedactionSpan(
                char_start=cs,
                char_end=ce,
                word_start=ws,
                word_end=we,
                category="pii",
                entity_type=entity_type,
                detector="presidio",
                confidence=float(score),
            )
        )
    return spans


def detect_pii_batch(
    items: list[tuple[str, list[dict] | None]], cfg: dict
) -> list[list[RedactionSpan]] | None:
    """Analyze many segments in parallel; ``None`` means the caller should loop instead.

    Presidio's analyze() is 82% of a redaction scan and does not thread (spaCy holds the GIL),
    so this fans the chunks across a persistent process pool — measured 7.5x on a 1077-segment
    transcript with byte-identical results. Returning ``None`` rather than partial output keeps
    the fail-closed contract: a scan that could not run must never look like a clean one.
    """
    if not items:
        return []
    use_gliner = bool(cfg.get("pii_use_gliner", C.REDACTION_PII_USE_GLINER))
    if use_gliner:
        return None  # GLiNER adds a second model per worker; not worth the memory
    if _get_analyzer(False) is None:
        raise DetectorUnavailableError(
            "Presidio analyzer could not be built (missing dependency, spaCy model "
            "or weights); these texts were never examined for PII"
        )

    from app.services.redaction import pii_pool

    # Flatten to chunks exactly as detect_pii would, remembering where each came from.
    chunks: list[str] = []
    origin: list[tuple[int, int]] = []  # (item index, char offset)
    for idx, (text, _words) in enumerate(items):
        if not text or not text.strip():
            continue
        for base in range(0, len(text), _MAX_CHARS):
            chunks.append(text[base : base + _MAX_CHARS])
            origin.append((idx, base))

    raw_all = pii_pool.analyze_texts(chunks)
    if raw_all is None:
        return None

    keep_entities = set(cfg.get("pii_entities", C.REDACTION_PII_ENTITIES))
    min_score = float(cfg.get("pii_confidence", C.DEFAULT_REDACTION_PII_CONFIDENCE))
    offsets_by_item = {
        idx: build_word_offsets(items[idx][0], items[idx][1]) for idx, _ in set(origin)
    }
    out: list[list[RedactionSpan]] = [[] for _ in items]
    for (idx, base), raw in zip(origin, raw_all, strict=True):
        out[idx].extend(_spans_from_raw(raw, base, offsets_by_item[idx], keep_entities, min_score))
    return out
