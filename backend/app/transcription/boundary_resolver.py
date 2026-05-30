"""Boundary smoothing for per-word speaker labels (issue #193).

Fixes the 1-3 word "wrong-speaker bleed" at turn boundaries: PyAnnote interval
boundaries are quantized (~200-250 ms), so a word straddling a turn gets assigned by
pure max-overlap to whichever side wins on that one word, and ``resegment_by_speaker``
then faithfully splits on the wrong label, producing a visible wrong-speaker island.

This runs AFTER ``assign_speakers()`` and BEFORE ``resegment_by_speaker()`` (wired at the
``finalize_segments`` chokepoint in ``segment_postprocess``). It is a pure function of the
word-level speaker labels + a small, legible config — no audio, no model. On by default
(the benchmark justified it: -32% WSER on the reporter's clip, AMI-regression-safe).

Phase 1 (rules): collapse a short island of speaker B that is sandwiched between two
longer runs of the SAME other speaker A, when there is no real pause at either seam.
Phase 2 (optional, ``margin_threshold > 0``): also collapse a longer island when every
word in it is "disputed" — i.e. carries a small top1-top2 overlap margin attached by
``assign_speakers`` as ``word["_overlap_margin"]``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MARGIN_KEY = "_overlap_margin"  # transient per-word key set by assign_speakers (Phase 2)


@dataclass
class BoundarySmoothingConfig:
    """Config for the boundary smoother. DB ``engine.boundary_*`` → env → defaults.

    Attributes:
        enabled: Master switch. On by default — ships the validated issue #193 boundary fix
            (-32% WSER on the reporter's clip, AMI-regression-safe). Toggle in the admin
            Engine panel (DB) or ENGINE_BOUNDARY_SMOOTHING_ENABLED.
        max_island_words: Max length (words) of a wrong-speaker island to collapse.
        max_island_duration: Max wall-clock duration (s) of an island to collapse.
        min_flank_words: Each flanking same-speaker run must be at least this long.
        min_silent_gap: If the silence between the island and either flank exceeds this
            (s), treat it as a real turn and leave it alone.
        margin_threshold: Phase 2. If > 0, also collapse islands longer than
            ``max_island_words`` when every island word's overlap margin is ≤ this value
            (genuinely ambiguous). 0 disables Phase 2.
    """

    enabled: bool = True
    max_island_words: int = 3
    max_island_duration: float = 1.5
    min_flank_words: int = 3
    min_silent_gap: float = 0.4
    margin_threshold: float = 0.0
    # Phase 3 acoustic re-check (runs in the GPU engine stage; off by default — adds an
    # embedding pass per short disputed/overlap word).
    acoustic_recheck_enabled: bool = False
    acoustic_cosine_margin: float = 0.05
    acoustic_max_word_dur: float = 1.0

    @classmethod
    def from_db_env(cls, db: Any = None) -> BoundarySmoothingConfig:
        """Resolve config from DB SystemSettings → ``ENGINE_BOUNDARY_*`` env → defaults.

        ``db=None`` (e.g. in the benchmark harness or unit tests) uses env/defaults only.
        """
        defaults = cls()

        def _bool(key: str, env: str, default: bool) -> bool:
            env_default = os.getenv(env)
            base = env_default.lower() == "true" if env_default is not None else default
            if db is None:
                return base
            from app.services.system_settings_service import get_setting_bool

            return get_setting_bool(db, key, default=base)

        def _num(key: str, env: str, default: float, cast: Any) -> Any:
            raw: Any = None
            if db is not None:
                from app.services.system_settings_service import get_setting

                raw = get_setting(db, key)
            if raw is None:
                raw = os.getenv(env)
            if raw is None or raw == "":
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                logger.warning("Invalid %s=%r, using default %r", key, raw, default)
                return default

        return cls(
            enabled=_bool(
                "engine.boundary_smoothing_enabled",
                "ENGINE_BOUNDARY_SMOOTHING_ENABLED",
                defaults.enabled,
            ),
            max_island_words=_num(
                "engine.boundary_max_island_words",
                "ENGINE_BOUNDARY_MAX_ISLAND_WORDS",
                defaults.max_island_words,
                int,
            ),
            max_island_duration=_num(
                "engine.boundary_max_island_duration",
                "ENGINE_BOUNDARY_MAX_ISLAND_DURATION",
                defaults.max_island_duration,
                float,
            ),
            min_flank_words=_num(
                "engine.boundary_min_flank_words",
                "ENGINE_BOUNDARY_MIN_FLANK_WORDS",
                defaults.min_flank_words,
                int,
            ),
            min_silent_gap=_num(
                "engine.boundary_min_silent_gap",
                "ENGINE_BOUNDARY_MIN_SILENT_GAP",
                defaults.min_silent_gap,
                float,
            ),
            margin_threshold=_num(
                "engine.boundary_margin_threshold",
                "ENGINE_BOUNDARY_MARGIN_THRESHOLD",
                defaults.margin_threshold,
                float,
            ),
            acoustic_recheck_enabled=_bool(
                "engine.boundary_acoustic_recheck_enabled",
                "ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED",
                defaults.acoustic_recheck_enabled,
            ),
            acoustic_cosine_margin=_num(
                "engine.boundary_acoustic_cosine_margin",
                "ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN",
                defaults.acoustic_cosine_margin,
                float,
            ),
            acoustic_max_word_dur=_num(
                "engine.boundary_acoustic_max_word_dur",
                "ENGINE_BOUNDARY_ACOUSTIC_MAX_WORD_DUR",
                defaults.acoustic_max_word_dur,
                float,
            ),
        )


def smooth_word_speakers(
    segments: list[dict[str, Any]],
    cfg: BoundarySmoothingConfig,
) -> list[dict[str, Any]]:
    """Collapse short wrong-speaker islands in per-word speaker labels, in place.

    Mutates the ``speaker`` field on the same word dicts referenced by ``segments`` and
    returns ``segments``. Idempotent: re-running on already-smoothed output is a no-op.
    """
    # Flatten words across segments, keeping references so writes propagate.
    flat = [
        w for s in segments for w in s.get("words", []) or [] if "speaker" in w and "start" in w
    ]
    if len(flat) < 3:
        return segments

    spk = [w["speaker"] for w in flat]
    st = [w["start"] for w in flat]
    en = [w.get("end", w["start"]) for w in flat]

    # Build maximal same-speaker runs as (start_idx, end_idx, speaker).
    runs: list[tuple[int, int, Any]] = []
    i = 0
    n = len(spk)
    while i < n:
        j = i
        while j < n and spk[j] == spk[i]:
            j += 1
        runs.append((i, j, spk[i]))
        i = j

    fixed = 0
    for r in range(1, len(runs) - 1):
        s_i, e_i, island_spk = runs[r]
        left, right = runs[r - 1], runs[r + 1]

        # Flanks must be the SAME other speaker (covers 3+ speaker cases).
        if left[2] != right[2] or left[2] == island_spk:
            continue
        # Flanks must each be a genuine run, not themselves a flicker.
        if (left[1] - left[0]) < cfg.min_flank_words or (right[1] - right[0]) < cfg.min_flank_words:
            continue
        # No real pause at either seam (a pause is evidence of a true turn / backchannel).
        if (st[s_i] - en[left[1] - 1]) > cfg.min_silent_gap:
            continue
        if (st[right[0]] - en[e_i - 1]) > cfg.min_silent_gap:
            continue

        island_len = e_i - s_i
        island_dur = en[e_i - 1] - st[s_i]
        within_phase1 = island_len <= cfg.max_island_words and island_dur <= cfg.max_island_duration
        # Phase 2: a longer island still collapses if every word is genuinely ambiguous.
        disputed = (
            cfg.margin_threshold > 0
            and island_dur <= cfg.max_island_duration
            and all(
                flat[k].get(_MARGIN_KEY, float("inf")) <= cfg.margin_threshold
                for k in range(s_i, e_i)
            )
        )
        if not (within_phase1 or disputed):
            continue

        for k in range(s_i, e_i):
            flat[k]["speaker"] = left[2]
        fixed += 1

    if fixed:
        logger.info("boundary_resolver: collapsed %d wrong-speaker island(s)", fixed)
    return segments


def _l2(vec: Any) -> Any:
    """L2-normalize a 1-D embedding (numpy)."""
    import numpy as np

    v = np.asarray(vec, dtype=np.float64).ravel()
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def acoustic_recheck(
    words: list[dict[str, Any]],
    centroids: dict[str, Any],
    embed_fn: Any,
    *,
    overlap_regions: list[dict[str, Any]] | None = None,
    margin_threshold: float = 0.05,
    cosine_margin: float = 0.05,
    pad: float = 0.10,
    max_word_dur: float = 1.0,
) -> int:
    """Phase 3 (issue #193): reassign short disputed/overlap words by voiceprint.

    The boundary smoother cannot fix "backchannel absorption" — a short "yeah"/"mm-hmm"
    by the listening speaker that diarization absorbed into the dominant speaker's segment
    (it is not an island). This re-checks the *acoustics*: for each candidate word, embed
    its audio window and reassign it to the speaker whose centroid it best matches.

    Strictly a RELABELING — it only changes the ``speaker`` of words that already exist;
    it never adds, removes, or invents a word (honors "don't assume backchannels unless
    the transcription says so").

    Candidates are short words (≤ ``max_word_dur`` s) that are either Phase-2 *disputed*
    (small ``_overlap_margin``) or fall inside a diarization *overlap* region — the two
    places absorption happens. A word is reassigned only when another speaker's centroid is
    cosine-closer by at least ``cosine_margin``.

    Args:
        words: flattened word dicts with ``speaker``/``start``/``end`` (mutated in place).
        centroids: ``{speaker_label: embedding}`` (e.g. the diarizer's native_embeddings).
        embed_fn: ``(start: float, end: float) -> embedding | None`` — embeds an audio
            window with the SAME model that produced ``centroids``. Returns None on failure.
        overlap_regions: optional ``[{start,end}, ...]`` from diarization overlap detection.
        margin_threshold: max ``_overlap_margin`` for a word to count as disputed.
        cosine_margin: min cosine improvement over the current speaker to reassign.
        pad: seconds of context added on each side of the word window.
        max_word_dur: only re-check words at or below this duration (backchannel-length).

    Returns:
        Number of words reassigned.
    """
    import numpy as np

    labels = [lab for lab in centroids if centroids[lab] is not None]
    if len(labels) < 2 or embed_fn is None:
        return 0
    cmat = np.stack([_l2(centroids[lab]) for lab in labels])  # (K, D)
    overlaps = overlap_regions or []
    reassigned = 0
    for w in words:
        s, e = w.get("start"), w.get("end")
        if s is None or e is None or (float(e) - float(s)) > max_word_dur:
            continue
        disputed = float(w.get(_MARGIN_KEY, float("inf"))) <= margin_threshold
        in_overlap = any(o["start"] < float(e) and float(s) < o["end"] for o in overlaps)
        if not (disputed or in_overlap):
            continue
        vec = embed_fn(max(0.0, float(s) - pad), float(e) + pad)
        if vec is None:
            continue
        sims = cmat @ _l2(vec)  # cosine (both L2-normalized)
        best = int(np.argmax(sims))
        cur = w.get("speaker")
        cur_sim = float(sims[labels.index(cur)]) if cur in labels else -1.0
        if labels[best] != cur and (float(sims[best]) - cur_sim) >= cosine_margin:
            w["speaker"] = labels[best]
            reassigned += 1
    if reassigned:
        logger.info("acoustic_recheck: reassigned %d short word(s) by voiceprint", reassigned)
    return reassigned
