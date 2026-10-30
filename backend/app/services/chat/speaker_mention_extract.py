"""Candidate-name extraction from a chat question (no database, no model).

Two tracks: Latin capitalization heuristics (:func:`extract_candidates`) and a
script-aware track (:func:`extract_script_aware_candidates`) for scripts where
capitalization carries no signal. Matching the candidates is
:mod:`speaker_name_match`'s job.
"""

from __future__ import annotations

import re

from app.services.chat.speaker_name_text import _CASELESS_SPACED_RE
from app.services.chat.speaker_name_text import _HANGUL_RE
from app.services.chat.speaker_name_text import _NO_SPACE_RUN_RE
from app.services.chat.speaker_name_text import _TOKEN_RE

# ---------------------------------------------------------------------------
# Candidate extraction — English-first capitalization heuristics.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

#: Common English words that are ALSO real first names ("Will", "Grace",
#: "Hope", "May", ...). A capitalized instance of one of these at the very
#: start of a sentence is orthographic convention, not evidence of a proper
#: noun — English capitalizes the first word of every sentence regardless.
#: Mid-sentence capitalization of the same word ("Did Grace present the
#: report?") IS evidence, because nothing else would capitalize it there.
_COMMON_WORD_NAMES: frozenset[str] = frozenset(
    {
        "will",
        "grace",
        "hope",
        "faith",
        "may",
        "june",
        "art",
        "bill",
        "rose",
        "jean",
        "victor",
        "hunter",
        "chase",
        "summer",
        "autumn",
        "dawn",
        "sky",
        "pat",
        "chris",
        "drew",
        "jordan",
        "robin",
        "dale",
        "mark",
        "gene",
        "frank",
        "sandy",
        "joy",
        "rich",
        "max",
        "ray",
        "gus",
        "trip",
    }
)

#: Question words, auxiliaries and pronouns that open the overwhelming
#: majority of chat questions and are for practical purposes NEVER a
#: person's display name. Unlike :data:`_COMMON_WORD_NAMES`, these are
#: excluded UNCONDITIONALLY — not just at sentence start — because a word
#: here is never plausible evidence either way, so there is no mid-sentence
#: exception to make. This is what stops "Did Grace present the report?"
#: from merging into the single multi-word candidate "Did Grace": without
#: it, the run-building loop below has no reason not to join two adjacent
#: capitalized words regardless of whether the first one is a name at all.
_NEVER_NAME_WORDS: frozenset[str] = frozenset(
    {
        "i",
        "we",
        "you",
        "they",
        "he",
        "she",
        "it",
        "did",
        "do",
        "does",
        "what",
        "who",
        "how",
        "when",
        "where",
        "why",
        "which",
        "is",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "can",
        "could",
        "should",
        "would",
        "the",
        "this",
        "that",
        "these",
        "those",
        "please",
        "tell",
        "give",
        "describe",
        "explain",
        "list",
        "show",
    }
)


def _is_sentence_initial(text: str, pos: int) -> bool:
    """Whether the word starting at ``pos`` opens ``text`` or follows ``. ! ?``.

    Deliberately simple: strip trailing whitespace from everything before
    ``pos`` and check whether what remains ends in sentence-ending
    punctuation (or is empty, i.e. this is the very first word). A capitalized
    common-word candidate ("Grace, can you help?") that opens its own sentence
    is therefore NOT treated as a name — English capitalizes sentence-initial
    words regardless of part of speech, so that position carries no signal by
    itself. This is a known, accepted false negative: resolving it needs
    context this deterministic pass does not have.
    """
    prefix = text[:pos]
    tail = prefix.rstrip()
    if not tail:
        return True
    return tail[-1] in ".!?"


def extract_candidates(text: str) -> list[str]:
    """Capitalized-word candidate phrases, in the order they appear.

    Multi-word runs of consecutive capitalized tokens (only whitespace
    between them) are extracted as ONE phrase — "Alice Chen" is a single
    candidate, never two — because multi-word names are first-class and
    splitting them would only ever produce a worse match. A single-token
    candidate that is also a common English word (:data:`_COMMON_WORD_NAMES`)
    is dropped unless it is capitalized **mid-sentence** (see
    :func:`_is_sentence_initial`).

    Returns:
        Candidate phrases, duplicates included (the caller dedupes by
        normalized form so repeated mentions cost one match, not N).
    """
    matches = list(_WORD_RE.finditer(text))
    candidates: list[str] = []
    i = 0
    n = len(matches)
    while i < n:
        m = matches[i]
        word = m.group(0)
        if not word[0].isupper():
            i += 1
            continue
        if word.lower() in _NEVER_NAME_WORDS:
            # Never a plausible name, at any position — and critically, never
            # a valid START of a multi-word run either, or "Did Grace" would
            # merge into one candidate before the mid-sentence check for
            # "Grace" alone ever gets a chance to run.
            i += 1
            continue

        run_words = [word]
        run_end = m.end()
        j = i + 1
        while j < n:
            gap = text[run_end : matches[j].start()]
            nxt = matches[j].group(0)
            if gap.strip() == "" and nxt[0].isupper() and nxt.lower() not in _NEVER_NAME_WORDS:
                run_words.append(nxt)
                run_end = matches[j].end()
                j += 1
            else:
                break

        if len(run_words) > 1:
            candidates.append(" ".join(run_words))
            i = j
            continue

        if word.lower() in _COMMON_WORD_NAMES and _is_sentence_initial(text, m.start()):
            i += 1
            continue

        candidates.append(word)
        i += 1

    return candidates


def _no_space_runs(text: str) -> list[str]:
    """Maximal runs of scriptio-continua characters in *text*.

    Stands in for word-boundary extraction, which these scripts do not have
    (``\\b`` never matches inside a run of them). A run is typically the
    closest thing to a "word" available without a segmenter — often longer
    than the name it contains, since nothing marks where the name ends and
    the next word begins. :func:`match_candidate`'s grapheme-prefix rung is
    what recovers the name from a run like "达娜说了什么" ("what did Dana say").
    """
    return [m.group(0) for m in _NO_SPACE_RUN_RE.finditer(text)]


def extract_script_aware_candidates(text: str) -> list[str]:
    """Non-Latin candidate phrases that :func:`extract_candidates` cannot see at all.

    ``extract_candidates``'s ``_WORD_RE`` only matches ``[A-Za-z]``, so a
    Chinese, Japanese, Korean, Thai, Arabic or Hindi name in the question was
    previously never even attempted against the roster — independent of
    anything the matching ladder does, because no candidate reached it.

    Two extraction strategies, chosen by script:

    - **Scriptio continua** (CJK ideographs, kana, Thai/Lao/Myanmar/Khmer):
      candidates are maximal runs (:func:`_no_space_runs`), broken at the
      first character outside that script.
    - **Spaced, caseless scripts** (Hangul, Arabic, Hebrew, Devanagari): the
      text already has whitespace-delimited tokens, so ordinary tokenization
      applies. Unlike the Latin pass there is no common-word/sentence-initial
      filter here — that filter exists specifically to use a capitalization
      signal these scripts do not carry. Every token becomes a candidate, and
      the matching ladder's ambiguity rule (a tie declines rather than
      guesses) is what stands in for it.

    Args:
        text: The user's question, as typed.

    Returns:
        Candidate phrases, duplicates included — the caller
        (:func:`resolve_speaker_mentions`) dedupes by normalized form, the
        same as :func:`extract_candidates`'s output.
    """
    candidates: list[str] = list(_no_space_runs(text))
    for token in _TOKEN_RE.findall(text):
        if _HANGUL_RE.search(token) or _CASELESS_SPACED_RE.search(token):
            candidates.append(token)
    return candidates
