"""QMSum adapter — 232 meetings, 1,576 gold-span queries, MIT (Tier A).

QMSum ships each meeting as one JSON object with ``meeting_transcripts``
(``{speaker, content}`` turns) and query lists whose ``relevant_text_span``
addresses those turns **by index**. The turn index is therefore load-bearing:
it is preserved onto every emitted segment and into the manifest's turn table so
the harness can map a gold span onto the chunks the production indexer produced.

QMSum carries no timestamps. 196 of its 232 meetings are redistributed AMI
(``Product``, 137) or ICSI (``Academic``, 59) recordings, both of which do have
word-level times, so this adapter recovers real timings for those by content
alignment (see :mod:`..nxt`). The 36 ``Committee`` meetings are Welsh/Canadian
parliamentary records with no timed source and get synthetic times.
"""

from __future__ import annotations

import json
import re
from functools import cached_property
from pathlib import Path

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.model import TIMING_REAL
from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn
from app.scripts.corpus_injection.nxt import align_turns_to_channels
from app.scripts.corpus_injection.nxt import read_meeting_channels

DOMAINS = ("Academic", "Product", "Committee")

# AMI role code -> the label QMSum uses for that participant. AMI's scenario
# meetings always have exactly these four roles.
AMI_ROLE_TO_QMSUM = {
    "PM": "Project Manager",
    "ID": "Industrial Designer",
    "UI": "User Interface",
    "ME": "Marketing",
}

# ICSI needs no table: QMSum's Academic speaker labels are "<role> <channel>"
# ("Grad C", "PhD D", "Postdoc E"), and that trailing letter IS the NXT channel.
_ICSI_CHANNEL_RE = re.compile(r"\b([A-Z])$")

_QMSUM_DIR_RE = re.compile(r"^QMSum-([0-9a-f]{7,40})$")


class QMSumAdapter(CorpusAdapter):
    """Parse ``$RAG_EVAL_DATA_DIR/qmsum``, optionally with AMI/ICSI timings."""

    key = "qmsum"

    def __init__(
        self,
        root: Path,
        ami_root: Path | None = None,
        icsi_root: Path | None = None,
    ) -> None:
        super().__init__(root)
        self.ami_root = Path(ami_root) if ami_root else None
        self.icsi_root = Path(icsi_root) if icsi_root else None

    # ---------------------------------------------------------------- layout

    @cached_property
    def _extracted(self) -> Path:
        """The single ``QMSum-<commit>/`` directory inside the corpus root."""
        if (self.root / "data").is_dir():
            return self.root
        candidates = sorted(p for p in self.root.iterdir() if p.is_dir() and (p / "data").is_dir())
        if not candidates:
            raise FileNotFoundError(f"No extracted QMSum tree with a data/ dir under {self.root}")
        return candidates[-1]

    @cached_property
    def _version(self) -> str:
        match = _QMSUM_DIR_RE.match(self._extracted.name)
        return match.group(1) if match else self._extracted.name

    def describe(self) -> CorpusInfo:
        return CorpusInfo(
            key=self.key,
            name="QMSum (Yale-LILY)",
            version=self._version,
            license_tier="A",
            root=str(self._extracted),
            citation="Zhong et al., QMSum, NAACL 2021 (arXiv:2104.05938). MIT. "
            "Transcripts derive from AMI / ICSI (CC BY 4.0) and parliamentary records.",
        )

    def _meeting_paths(self) -> dict[str, tuple[str, Path]]:
        found: dict[str, tuple[str, Path]] = {}
        for domain in DOMAINS:
            for path in sorted((self._extracted / "data" / domain / "all").glob("*.json")):
                found[path.stem] = (domain, path)
        return found

    def meeting_ids(self) -> list[str]:
        return sorted(self._meeting_paths())

    # ------------------------------------------------------- timing sources

    @cached_property
    def _ami_channels(self) -> dict[str, dict[str, str]]:
        """meeting id -> {QMSum speaker label: NXT channel letter}, from AMI.

        A thin, QMSum-role-keyed projection of :func:`~..nxt.read_meeting_channels` — every
        distractor meeting (:class:`~.ami.AMIDistractorAdapter`) needs the same
        ``meetings.xml`` parse but keyed by channel letter, since most of them carry no
        ``role`` attribute at all (see that function's docstring).
        """
        if not self.ami_root:
            return {}
        meetings_xml = self._find(self.ami_root, "corpusResources/meetings.xml")
        if meetings_xml is None:
            return {}
        channels_by_meeting = read_meeting_channels(meetings_xml)
        return {
            meeting_id: {
                AMI_ROLE_TO_QMSUM[info["role"]]: channel
                for channel, info in channels.items()
                if info["role"] in AMI_ROLE_TO_QMSUM
            }
            for meeting_id, channels in channels_by_meeting.items()
        }

    @cached_property
    def _ami_words_dir(self) -> Path | None:
        return self._find(self.ami_root, "words") if self.ami_root else None

    @cached_property
    def _icsi_words_dir(self) -> Path | None:
        return self._find(self.icsi_root, "ICSIplus/Words") if self.icsi_root else None

    @staticmethod
    def _find(base: Path | None, suffix: str) -> Path | None:
        """Locate ``suffix`` under ``base``, tolerating one archive-name level.

        The fetcher extracts to ``ami/ami_public_manual_1.6.2/`` and
        ``icsi/ICSI_plus_NXT/``; accepting either the corpus dir or the
        extracted dir keeps the CLI from needing version-pinned paths.
        """
        if base is None:
            return None
        direct = base / suffix
        if direct.exists():
            return direct
        for child in sorted(p for p in base.iterdir() if p.is_dir()):
            if (child / suffix).exists():
                return child / suffix
        return None

    def _channel_files(self, meeting_id: str, domain: str, speakers: list[str]) -> dict[str, Path]:
        if domain == "Product" and self._ami_words_dir:
            mapping = self._ami_channels.get(meeting_id, {})
            return {
                speaker: self._ami_words_dir / f"{meeting_id}.{channel}.words.xml"
                for speaker, channel in mapping.items()
                if speaker in speakers
            }
        if domain == "Academic" and self._icsi_words_dir:
            files = {}
            for speaker in speakers:
                match = _ICSI_CHANNEL_RE.search(speaker.strip())
                if match:
                    files[speaker] = (
                        self._icsi_words_dir / f"{meeting_id}.{match.group(1)}.words.xml"
                    )
            return files
        return {}

    # ------------------------------------------------------------- loading

    def load(self, meeting_id: str) -> MeetingDoc:
        domain, path = self._meeting_paths()[meeting_id]
        raw = json.loads(path.read_text(encoding="utf-8"))
        turns = [
            Turn(turn_index=i, speaker=t["speaker"].strip(), text=t["content"].strip())
            for i, t in enumerate(raw.get("meeting_transcripts", []))
        ]

        doc = MeetingDoc(
            corpus=self.key,
            meeting_id=meeting_id,
            title=f"QMSum {domain} — {meeting_id}",
            turns=turns,
            language="en",
            extra={
                "domain": domain,
                "specific_query_count": len(raw.get("specific_query_list", [])),
                "general_query_count": len(raw.get("general_query_list", [])),
                "topic_count": len(raw.get("topic_list", [])),
                "license_tier": "A",
            },
        )

        channel_files = self._channel_files(meeting_id, domain, doc.speakers)
        if channel_files:
            reference = "ami" if domain == "Product" else "icsi"
            timed, matched, total = align_turns_to_channels(turns, channel_files)
            doc.timing = TimingInfo(
                source=TIMING_REAL,
                reference=f"{reference}:{meeting_id}",
                aligned_turns=timed,
                total_turns=len(turns),
            )
            doc.extra["token_match_rate"] = round(matched / total, 4) if total else 0.0
        return doc
