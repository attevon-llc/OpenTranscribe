"""Corpus loading, tokenisation, and exact phrase location.

One tokeniser serves both the ground-truth validator and the BM25 characterisation, so a
phrase that the validator says occurs in exactly one meeting is the same phrase BM25 sees.
It keeps digit groups, currency and hyphenated ids intact (``$120,311``, ``ops-50017``,
``v4.1.2``) because those are precisely the anchors whose uniqueness the whole tier rests
on; a naive ``[a-z0-9]+`` split would shatter ``10,000`` into ``10`` and ``000`` and lose
the rarity that makes narrowing exact.

``find_phrase`` is an **index-narrowed exact scan**: the token index supplies a superset
of candidate documents (any document containing the phrase must contain all its tokens),
and each candidate is then confirmed with a boundary-anchored regex. It cannot miss an
occurrence, and ``find_phrase_naive`` exists so the tests can prove that on a small corpus
by comparing against a full linear scan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"[A-Za-z0-9$][A-Za-z0-9,.$'\-]*")
#: Left boundary rejects a match that starts mid-number ("10,000" inside "110,000");
#: right boundary rejects one that ends mid-id ("OPS-5000" inside "OPS-50001").
_LEFT = r"(?<![0-9A-Za-z_,$-])"
_RIGHT = r"(?![0-9A-Za-z_-])"


def tokenize(text: str) -> list[str]:
    """Return lowercase tokens, trailing sentence punctuation stripped."""
    return [t for t in (m.group(0).lower().rstrip(".,") for m in _TOKEN_RE.finditer(text)) if t]


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Return the boundary-anchored, case-insensitive pattern for an exact phrase."""
    return re.compile(_LEFT + re.escape(phrase) + _RIGHT, re.IGNORECASE)


@dataclass
class Document:
    """One meeting, flattened for search."""

    file_uuid: str
    meeting_key: str
    series_id: str
    date: str
    text: str
    turn_texts: list[str]
    speakers: list[str]
    tokens: list[str]


class Corpus:
    """An in-memory view of a generated corpus's meetings."""

    def __init__(self, docs: list[Document]) -> None:
        """Build the token index over ``docs`` (ordered by ``file_uuid``)."""
        self.docs = {d.file_uuid: d for d in sorted(docs, key=lambda d: d.file_uuid)}
        self.order = list(self.docs)
        self.token_index: dict[str, set[str]] = {}
        self.term_freq: dict[str, dict[str, int]] = {}
        for doc in self.docs.values():
            freqs: dict[str, int] = {}
            for token in doc.tokens:
                freqs[token] = freqs.get(token, 0) + 1
            self.term_freq[doc.file_uuid] = freqs
            for token in freqs:
                self.token_index.setdefault(token, set()).add(doc.file_uuid)

    @classmethod
    def load(cls, corpus_dir: Path) -> Corpus:
        """Load every ``meetings/*.jsonl`` shard under ``corpus_dir``."""
        docs: list[Document] = []
        for shard in sorted(Path(corpus_dir).glob("meetings/*.jsonl")):
            with shard.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        docs.append(cls._to_document(json.loads(line)))
        if not docs:
            raise FileNotFoundError(f"no meeting shards under {corpus_dir}/meetings/")
        return cls(docs)

    @staticmethod
    def _to_document(meeting: dict) -> Document:
        turn_texts = [t["content"] for t in meeting["turns"]]
        text = "\n".join(f"{t['speaker']}: {t['content']}" for t in meeting["turns"])
        return Document(
            file_uuid=meeting["file_uuid"],
            meeting_key=meeting["meeting_key"],
            series_id=meeting["series_id"],
            date=meeting["date"],
            text=text,
            turn_texts=turn_texts,
            speakers=[s["name"] for s in meeting["speakers"]],
            tokens=tokenize(text),
        )

    def candidates(self, phrase: str) -> list[str]:
        """Return the documents that could contain ``phrase`` (a superset of matches)."""
        tokens = tokenize(phrase)
        if not tokens:
            return []
        postings = sorted((self.token_index.get(t, set()) for t in tokens), key=len)
        narrowed = set(postings[0])
        for posting in postings[1:]:
            narrowed &= posting
            if not narrowed:
                break
        return sorted(narrowed)

    def find_phrase(self, phrase: str) -> dict[str, int]:
        """Return ``{file_uuid: occurrences}`` for an exact, boundary-anchored phrase."""
        pattern = phrase_pattern(phrase)
        hits: dict[str, int] = {}
        for file_uuid in self.candidates(phrase):
            count = len(pattern.findall(self.docs[file_uuid].text))
            if count:
                hits[file_uuid] = count
        return hits

    def find_phrase_naive(self, phrase: str) -> dict[str, int]:
        """Full linear scan over every document — the control for :meth:`find_phrase`."""
        pattern = phrase_pattern(phrase)
        hits: dict[str, int] = {}
        for file_uuid, doc in self.docs.items():
            count = len(pattern.findall(doc.text))
            if count:
                hits[file_uuid] = count
        return hits

    def turn_contains(self, file_uuid: str, turn_index: int, phrase: str) -> bool:
        """True when the given turn of the given meeting contains ``phrase`` exactly."""
        doc = self.docs[file_uuid]
        if not 0 <= turn_index < len(doc.turn_texts):
            return False
        return phrase_pattern(phrase).search(doc.turn_texts[turn_index]) is not None


def load_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file into a list of dicts."""
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
