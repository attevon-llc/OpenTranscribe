"""MIRACL as an injectable corpus — one passage per file, one turn per passage (#453).

MIRACL is the anchor for every non-English retrieval claim: 18 languages, Apache-2.0,
**human pooled judgements with explicit negatives**. Nothing else on the NAS has all
three, and without it no multilingual change in #453 can be measured at all.

⚠️ **A "meeting" here is a single Wikipedia passage, and that is the whole design.**
Every other corpus in this harness ships multi-turn transcripts whose gold is an
inclusive *turn range*. MIRACL judges whole *passages* — "docid 8156619#0 is relevant
to query 10036600#0". Writing each passage as a one-turn file makes a document-level
judgement exactly ``GoldSpan(uuid, 0, 0)``, so ``qrels.py`` and ``metrics.py`` need no
second convention and the harness keeps ONE overlap rule.

⚠️ **Timing is synthetic and must stay that way.** A Wikipedia passage has no
recording, no speaker and no duration. ``TimingInfo`` defaults to
``TIMING_SYNTHETIC``, which is what stops these files feeding a duration, latency,
WER-by-time or diarization metric — every one of which would be meaningless here.

The subset is bounded by :func:`tests.eval.harness.miracl.build_subset`: extraction
streams a whole language corpus (measured 79.5 s for Spanish, 1.5 GB gzipped), so the
passages are selected from the judged set of a fixed query count and cached.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import Turn

#: Speaker label for a passage. MIRACL has no speakers; a constant keeps the
#: speaker-scoped code paths well-defined rather than fed an empty string.
_SPEAKER = "Passage"

#: Default number of dev queries whose judged passages get injected. Enough to
#: measure with, small enough that a language subset builds in one corpus scan.
DEFAULT_QUERY_COUNT = 200


class MiraclAdapter(CorpusAdapter):
    """One MIRACL language, as injectable one-passage documents."""

    def __init__(
        self,
        root: Path,
        language: str = "es",
        *,
        query_count: int = DEFAULT_QUERY_COUNT,
        split: str = "dev",
        cache_dir: Path | None = None,
    ) -> None:
        super().__init__(root)
        self.language = language
        self.query_count = query_count
        self.split = split
        self._cache_dir = cache_dir or Path(
            os.environ.get("MIRACL_SUBSET_CACHE", str(root / ".miracl-subsets"))
        )
        self._passages: dict | None = None

    def _load_subset(self) -> dict:
        """Topics/qrels/passages for this language, built once and cached."""
        if self._passages is None:
            # Imported lazily: the harness lives under tests/ and the injector must
            # stay importable in a container that ships no test tree.
            from tests.eval.harness import miracl

            _, _, passages = miracl.build_subset(
                self.root,
                self.language,
                query_count=self.query_count,
                split=self.split,
                cache_dir=self._cache_dir,
            )
            self._passages = passages
        return self._passages

    def describe(self) -> CorpusInfo:
        return CorpusInfo(
            key="miracl",
            name=f"MIRACL v1.0 ({self.language})",
            version="1.0",
            # Tier A: Apache-2.0 topics/qrels over CC BY-SA Wikipedia passages.
            # Publishable, with attribution if passage text is reproduced.
            license_tier="A",
            root=str(self.root),
            citation="Zhang et al., MIRACL: A Multilingual Retrieval Dataset (2023)",
        )

    def meeting_ids(self) -> list[str]:
        """The docids in this language's subset, in a stable order.

        Sorted so two injections of the same subset assign uuids in the same order —
        the manifest is the join between MIRACL's docids and the uuids the app
        indexed, and an unstable order would make two runs incomparable.
        """
        return sorted(self._load_subset())

    def load(self, meeting_id: str) -> MeetingDoc:
        """One passage as a single-turn document.

        The passage TITLE is carried as the document title rather than prepended to
        the text: the chunk index embeds ``"{title} | {date} | participants: …"`` as
        its contextualization header, so putting it in the title feeds that machinery
        exactly as a real recording would, instead of polluting the passage body that
        the gold judgement is about.
        """
        passage = self._load_subset()[meeting_id]
        return MeetingDoc(
            corpus="miracl",
            meeting_id=meeting_id,
            title=passage.title or meeting_id,
            language=self.language,
            turns=[Turn(turn_index=0, speaker=_SPEAKER, text=passage.text)],
            extra={
                "miracl_docid": passage.docid,
                "miracl_language": self.language,
                "miracl_split": self.split,
            },
        )
