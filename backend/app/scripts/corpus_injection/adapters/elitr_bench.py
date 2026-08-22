"""ELITR-Bench adapter — 18 noisy ASR meeting transcripts with 271 human QA pairs (#521).

ELITR-Bench (Thonet et al., COLING 2025, ``utter-project/ELITR-Bench``) is the first
corpus in this harness whose gold is **human-written question/answer pairs with a
dedicated ``who`` category** (96 of 271) — the speaker-attribution axis nothing else
we inject tests directly. Questions also carry ``answer-position`` metadata
(begin / start / middle / end) as a coarse gold-span proxy.

⚠️ **The licence is SPLIT, and the split is why this corpus is Tier B here.** The
benchmark's own ``LICENSE-DATA.txt`` is CC-BY-4.0, but it covers only the QA layer
(``data/elitr-bench-qa_*.json``). The transcripts are extracted from the
**ELITR-minuting-corpus** (lindat ``11234/1-4692``, CC BY-NC-SA 4.0) by the
upstream ``preparation/extract_transcripts.py`` — the same Tier-B artifact the NAS
already holds under ``elitr/``. Injected content therefore inherits the
transcripts' NC terms: internal eval only, metrics-only commits, prose never
leaves ``.rag-403/``/the NAS.

⚠️ **Speakers are de-identified tokens** (``(PERSON6)`` in transcripts,
``[PERSON3]`` in answers). The 96 ``who`` questions test attribution *mechanics*
against person-tokens, not role/name resolution the way AMI's annotation layers
do — record that caveat beside any number derived from them.

Transcript format: a line starting ``(PERSONn)`` opens that speaker's turn; a line
without a marker continues the current turn (the corpus's own convention — ASR
output with minimal manual correction, so expect disfluencies and mid-word
errors; that noise is the point of the benchmark). Five of the 18 files open with
unmarked lines before the first ``(PERSONn)``; those become an ``"Unknown"``
speaker turn rather than being dropped (content is retrieval haystack either way)
or attributed to a person the file never named. **``meeting_en_dev_006`` carries
no speaker markers at all** (its ``transcript_MAN2`` source variant is
unattributed) — measured, not assumed — so that one meeting injects as a single
``"Unknown"`` speaker; its 15 questions still work (none require attribution the
transcript itself cannot support).

Inline annotation tags (``<unintelligible/>`` ×1167 across the corpus,
``<laugh/>``, ``<censored/>``, ``<other_noise/>``, ``<parallel_talk/>``,
``<another_language/>``) are kept **verbatim**: upstream feeds these files to
models unmodified, transcript noise is what the benchmark exists to measure, and
stripping them here would make our numbers describe a cleaner corpus than anyone
else's.

No timestamps exist anywhere in the corpus, so every turn is left untimed and
``timings.resolve_timings`` stamps synthetic provenance — these files must never
feed a duration/latency metric.
"""

from __future__ import annotations

import json
import re
from functools import cached_property
from pathlib import Path

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import Turn

_SPEAKER_RE = re.compile(r"^\((PERSON\d+)\)\s*")

#: Label for content preceding the first speaker marker (5 of 18 files). A real
#: string the app treats as an ordinary speaker name — never a fabricated PERSONn.
_UNATTRIBUTED = "Unknown"


class ElitrBenchAdapter(CorpusAdapter):
    """Parse ``$RAG_EVAL_DATA_DIR/elitr-bench`` (``transcripts/*.txt`` + ``data/*.json``)."""

    key = "elitr-bench"

    # ------------------------------------------------------------------ layout

    @cached_property
    def _transcripts_dir(self) -> Path:
        path = self.root / "transcripts"
        if not path.is_dir():
            raise FileNotFoundError(
                f"ELITR-Bench transcripts not found at {path} — run the staging steps in "
                "issue #521 (upstream data.zip + preparation.extract_transcripts against "
                "the NAS ELITR-minuting-corpus)"
            )
        return path

    @cached_property
    def _qa_by_meeting(self) -> dict[str, list[dict[str, str]]]:
        """meeting id -> its QA records, from both splits (dev + test2)."""
        out: dict[str, list[dict[str, str]]] = {}
        for split in ("dev", "test2"):
            path = self.root / "data" / f"elitr-bench-qa_{split}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            for meeting in payload["meetings"]:
                out[meeting["id"]] = list(meeting["questions"])
        return out

    # ---------------------------------------------------------------- contract

    def describe(self) -> CorpusInfo:
        return CorpusInfo(
            key=self.key,
            name="ELITR-Bench (QA over ELITR-minuting transcripts)",
            version="elitr-bench-github-2026-08",
            # Tier B because of the TRANSCRIPTS (CC BY-NC-SA via ELITR-minuting),
            # not the QA layer (CC-BY-4.0) — see the module docstring.
            license_tier="B",
            root=str(self.root),
            citation=(
                "Thonet, Besacier & Rozen, 'ELITR-Bench: A Meeting Assistant Benchmark "
                "for Long-Context Language Models', COLING 2025"
            ),
        )

    def meeting_ids(self) -> list[str]:
        return sorted(p.stem for p in self._transcripts_dir.glob("*.txt"))

    def load(self, meeting_id: str) -> MeetingDoc:
        path = self._transcripts_dir / f"{meeting_id}.txt"
        turns: list[Turn] = []
        speaker: str | None = None
        buffer: list[str] = []

        def _flush() -> None:
            text = " ".join(buffer).strip()
            if text:
                turns.append(
                    Turn(
                        turn_index=len(turns),
                        speaker=speaker if speaker is not None else _UNATTRIBUTED,
                        text=text,
                    )
                )
            buffer.clear()

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            match = _SPEAKER_RE.match(line)
            if match:
                _flush()
                speaker = match.group(1)
                remainder = line[match.end() :].strip()
                if remainder:
                    buffer.append(remainder)
            else:
                buffer.append(line)
        _flush()

        return MeetingDoc(
            corpus=self.key,
            meeting_id=meeting_id,
            title=meeting_id.replace("_", " "),
            turns=turns,
            extra={"question_count": len(self._qa_by_meeting.get(meeting_id, []))},
        )
