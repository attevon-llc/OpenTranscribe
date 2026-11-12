"""Search and chat text drawn from the data a person can actually see, not random strings.

Locust-free. A real search term is a word or two a person read in a transcript, a speaker's name
or a tag; a real chat question is built around such a term. ``Phrasebook`` learns them from what
the persona's own pages return (segment text, speaker names, tag names, collection names), so a
run against the seeded library asks about the seeded library, and a run against a restored
real-looking library asks about that one. Nothing is generated at random except the choice among
phrases already seen, and the caller passes in the ``random.Random`` so a run is repeatable.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from typing import Any

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
STOP_WORDS = frozenset(
    [
        'the',
        'and',
        'for',
        'are',
        'but',
        'not',
        'you',
        'all',
        'any',
        'can',
        'had',
        'her',
        'was',
        'one',
        'our',
        'out',
        'has',
        'have',
        'this',
        'that',
        'with',
        'from',
        'they',
        'will',
        'would',
        'there',
        'their',
        'what',
        'about',
        'which',
        'when',
        'your',
        'said',
        'were',
        'been',
        'into',
        'than',
        'then',
        'them',
        'these',
        'those',
        'just',
        'like',
        'also',
        'very',
        'some',
        'such',
        'only',
        'over',
        'more',
        'most',
        'other',
        'how',
        'why',
        'who',
        'its',
        "it's",
        "don't",
        "i'm",
        "we're",
        "you're",
        "isn't",
    ]
)
#: Questions a researcher asks of a body of recordings; ``{}`` is a phrase seen in the data.
QUESTIONS = (
    'What was decided about {}?',
    'Summarize what people said about {}.',
    'Who raised {} and what did they ask for?',
    'Were there any action items about {}?',
    'How did the discussion of {} change over time?',
)
MAX_PHRASES = 400
#: What is asked before anything has been learned (the first search of a brand-new session).
FALLBACK = ('meeting', 'budget', 'schedule')


def content_words(text: str) -> list[str]:
    """The lower-cased words of ``text`` that carry meaning (no stop words, no tiny tokens)."""
    return [w.lower() for w in _WORD.findall(text) if w.lower() not in STOP_WORDS]


class Phrasebook:
    """Phrases learned from one person's own pages."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng: Any = rng or random
        self.phrases: list[str] = []
        self.names: list[str] = []

    def learn_texts(self, texts: Iterable[str]) -> None:
        """Take one- and two-word windows from transcript text."""
        for text in texts:
            words = content_words(text)
            for i, word in enumerate(words):
                self._add(word)
                if i + 1 < len(words):
                    self._add(f'{word} {words[i + 1]}')

    def learn_names(self, names: Iterable[str]) -> None:
        """Speaker, tag and collection names: people search for these verbatim."""
        for name in names:
            name = name.strip()
            if name and name not in self.names:
                self.names.append(name)

    def _add(self, phrase: str) -> None:
        if phrase not in self.phrases and len(self.phrases) < MAX_PHRASES:
            self.phrases.append(phrase)

    @property
    def empty(self) -> bool:
        return not (self.phrases or self.names)

    def term(self) -> str:
        """A search term: usually a phrase from a transcript, sometimes a name."""
        if self.names and (not self.phrases or self.rng.random() < 0.25):
            return self.rng.choice(self.names)
        return self.rng.choice(self.phrases or list(FALLBACK))

    def prefix(self) -> str:
        """What a person has typed when the suggestion box fires: the start of a term."""
        term = self.term()
        return term[: self.rng.randint(2, max(2, min(5, len(term))))]

    def question(self) -> str:
        """A chat question around a phrase from the data."""
        return self.rng.choice(QUESTIONS).format(self.term())
