"""Content recall of an answer against AMI's per-item human annotations.

Every metric this repo had before measured RETRIEVAL — did the right chunk rank,
did the scope get covered. None of them answered the question the product is
actually judged on: **is the fact the human wrote down present in the answer?**

AMI's abstractive layers are unusually well suited to that, and this module
exists because of the shape rather than in spite of it. A ``<decisions>`` or
``<actions>`` layer is a list of *discrete, independently checkable* statements
("The target selling price will be 25 Euro dollars"), one per line, each tagged
with the recording it came from. That makes per-item recall a real measurement
rather than a similarity score over two blobs of prose.

⚠️ **This is a FLOOR, deliberately, and must be reported as one.** It scores
lexical overlap, so an answer that conveys an item in genuinely different words
("priced at twenty-five euros" vs "25 Euro dollars") scores as a miss. It can
therefore under-report a good answer but it can NOT over-report a bad one, which
is the direction an automated metric must fail in when it is used to argue that
a change helped. Use it to rank arms and to find regressions cheaply; use a
calibrated judge (#518/#64) for the absolute number, never this.

The complementary trap is worse and is why there is no embedding-similarity
version here: a soft score conceals which SPECIFIC items were missed, and the
missed items are the finding. ``score_answer`` returns them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field

#: Tokens that carry no discriminating content. Deliberately SHORT: an
#: aggressive stop list starts deleting the words that distinguish one decision
#: from another ("no LCD" is two stopwords and an acronym), and a miss caused by
#: the metric's own vocabulary is indistinguishable from a miss by the model.
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "it",
        "its",
        "they",
        "them",
        "their",
        "he",
        "she",
        "his",
        "her",
        "we",
        "us",
        "our",
        "you",
        "your",
        "i",
        "me",
        "my",
        "not",
        "no",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "there",
        "here",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "also",
        "very",
        "more",
        "most",
        "some",
        "any",
        "each",
        "other",
        "another",
        "such",
        "own",
        "same",
        "so",
        "too",
        "s",
        "t",
        "d",
        "ll",
        "m",
        "o",
        "re",
        "ve",
        "y",
    ]
)

#: An item must contribute at least this many content tokens to be scoreable.
#: Below it, "Have a locator." reduces to {locator} and a single incidental
#: mention anywhere in a long answer would score it as recalled.
_MIN_ITEM_TOKENS = 2

#: Fraction of an item's content tokens that must appear in the answer for it to
#: count as recalled. 0.6 is a judgement call and is stated here rather than
#: buried: at 1.0 any paraphrase misses, at 0.4 two unrelated shared nouns pass.
#: It is a threshold on a FLOOR metric -- move it and you change what the floor
#: means, so re-report every arm rather than comparing across values.
_RECALL_THRESHOLD = 0.6

#: `[IS1004a] The project goal is ...` -- AMI reference lines carry the source
#: recording. The tag is stripped for scoring but kept so a miss can be reported
#: against the recording it came from.
_TAGGED_LINE = re.compile(r"^\s*\[([^\]]+)\]\s*(.+?)\s*$")


def _tokens(text: str) -> set[str]:
    """Content tokens of ``text``: lowercased, de-punctuated, stopwords removed."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOP}


@dataclass
class ItemScore:
    """One reference item and whether the answer recalled it."""

    recording: str
    text: str
    recalled: bool
    #: Fraction of the item's content tokens found in the answer. Reported even
    #: for a miss: a near-miss at 0.55 and a total miss at 0.05 are different
    #: findings, and collapsing both to False hides which one you have.
    overlap: float


@dataclass
class RecallScore:
    """An answer's recall over one reference's items."""

    items: list[ItemScore] = field(default_factory=list)
    #: Items too short to score. Counted, never silently dropped -- a recall of
    #: "3/3" over a reference whose other 9 items were skipped is a wrong number
    #: presented as a right one.
    skipped: int = 0

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def recalled(self) -> int:
        return sum(1 for i in self.items if i.recalled)

    @property
    def recall(self) -> float:
        return self.recalled / self.total if self.total else 0.0

    @property
    def missed(self) -> list[ItemScore]:
        """The items the answer did not carry -- the actual finding."""
        return [i for i in self.items if not i.recalled]

    def recordings_covered(self) -> set[str]:
        """Recordings for which at least one item was recalled.

        This is content-derived coverage, and it is the honest counterpart to
        the citation-derived ``files_consulted``: a model can discuss a
        recording's decisions perfectly and cite nothing, which the citation
        metric scores as a miss and this scores as covered.
        """
        return {i.recording for i in self.items if i.recalled}


def parse_reference_items(reference: str) -> list[tuple[str, str]]:
    """Split an AMI reference into ``(recording, item)`` pairs.

    Falls back to one untagged item per line for a reference that carries no
    ``[recording]`` tags (QMSum's single-file references are prose, not lists),
    so a caller need not know which corpus a reference came from.
    """
    pairs: list[tuple[str, str]] = []
    for line in (reference or "").splitlines():
        if not line.strip():
            continue
        match = _TAGGED_LINE.match(line)
        if match:
            pairs.append((match.group(1), match.group(2)))
        else:
            pairs.append(("", line.strip()))
    return pairs


def score_answer(answer: str, reference: str) -> RecallScore:
    """Score ``answer`` for per-item recall against an AMI ``reference``.

    Args:
        answer: The app's answer text.
        reference: The human annotation, one item per line, optionally tagged
            ``[recording]``.

    Returns:
        A :class:`RecallScore`. ``recall`` is a FLOOR -- see the module
        docstring before quoting it as an accuracy figure.
    """
    answer_tokens = _tokens(answer)
    score = RecallScore()
    for recording, text in parse_reference_items(reference):
        item_tokens = _tokens(text)
        if len(item_tokens) < _MIN_ITEM_TOKENS:
            score.skipped += 1
            continue
        overlap = len(item_tokens & answer_tokens) / len(item_tokens)
        score.items.append(
            ItemScore(
                recording=recording,
                text=text,
                recalled=overlap >= _RECALL_THRESHOLD,
                overlap=overlap,
            )
        )
    return score
