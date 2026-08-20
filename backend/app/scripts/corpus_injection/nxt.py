"""Read AMI/ICSI NXT word annotations and align QMSum turns onto them.

AMI and ICSI ship the same NXT format: one ``<meeting>.<channel>.words.xml`` per
speaker channel, each ``<w>`` carrying ``starttime``/``endtime``. QMSum
redistributes the *text* of those same meetings as ``{speaker, content}`` turns
with the timings stripped, and 196 of its 232 meetings come from one of the two.
Recovering the times means matching QMSum's turns back onto the timed words.

**Do not align by segment index.** The obvious approach — zip QMSum's turn list
against the reference's segment list — is wrong, and measurably so: ES2004a has
320 QMSum turns against 283 AMI segments, IS1003b has 407 against 454. QMSum
merged some segments and split others. Index alignment silently assigns times
from an unrelated part of the meeting, and nothing downstream would notice.

What *is* stable is the per-speaker word sequence. QMSum preserves each
speaker's words in order, so this module aligns **speaker run by speaker run, on
content**: map each QMSum speaker label to its reference channel, concatenate
that speaker's QMSum tokens, diff them against that channel's timed tokens, and
carry the times back to whichever turn each matched token came from. Insertions
and deletions on either side (QMSum drops ``{disfmarker}`` markers; NXT emits
punctuation and vocal-sound elements as separate nodes) fall out of the diff.

**XML trust (bandit B405/B314).** The stdlib parser is used deliberately. These
files are not user input: they arrive only from ``scripts/fetch-rag-eval-data.sh``,
which verifies a pinned SHA-256 for every archive, they are read by a developer
CLI rather than any request path, and ``defusedxml`` is not a pinned dependency
of this project (it is present only transitively, so importing it would work on a
developer's venv and fail in a clean install). If either of those facts changes —
an adapter that parses XML from an unverified source, or this code moving into a
request path — switch to ``defusedxml`` and add it to ``requirements.txt``.
"""

from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET  # noqa: S405  # nosec B405 — see module note on XML trust
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.scripts.corpus_injection.model import Turn
from app.scripts.corpus_injection.model import Word

# QMSum inline annotation markers: {disfmarker}, {vocalsound}, {gap}, {pause}, ...
_MARKER_RE = re.compile(r"\{[^}]*\}")
_TOKEN_CLEAN_RE = re.compile(r"[^a-z0-9']")


@dataclass(slots=True)
class TimedToken:
    token: str
    start: float
    end: float
    surface: str


def normalize_token(raw: str) -> str:
    """Lower-case and strip everything but letters, digits and apostrophes.

    Both corpora disagree about punctuation and casing but agree about letters,
    so normalising to that is what makes the diff line up.
    """
    return _TOKEN_CLEAN_RE.sub("", raw.lower().replace("’", "'"))


def read_meeting_channels(meetings_xml: Path) -> dict[str, dict[str, dict[str, str]]]:
    """Parse AMI's ``corpusResources/meetings.xml`` into per-meeting channel metadata.

    Returns ``{observation_id: {nxt_channel_letter: {"role": ..., "global_name": ...}}}``.
    Shared by :class:`~.adapters.qmsum.QMSumAdapter` (which needs the QMSum-role-labelled
    subset of this — see ``AMI_ROLE_TO_QMSUM``) and :class:`~.adapters.ami.AMIDistractorAdapter`
    (which needs every channel, not just the four-role subset).

    ``role`` is only populated for AMI's four-role Scenario/Product meetings
    (``PM``/``ID``/``UI``/``ME``) — a meeting recorded outside that protocol has no ``role``
    attribute on its `<speaker>` entries at all (checked against the real corpus: 33 of the 34
    Product-excluded meetings), so a caller that needs a speaker label for every meeting MUST
    fall back to something else. ``global_name`` is present for every meeting checked but is
    still not guaranteed by the schema, hence also optional here (empty string, never absent).

    ⚠️ **`meetings.xml`'s own `<speaker>` list undercounts a real meeting's channels** — IN1001
    lists 3 speakers (A/B/C) but ships 4 channels' worth of `segments.xml`/`words.xml` (A-D).
    Channel *presence* must come from the `segments`/`words` directory listing, never from this
    file; this function exists only to attach a friendlier label where one exists.
    """
    root = ET.parse(meetings_xml).getroot()  # noqa: S314  # nosec B314 — see module note on XML trust
    out: dict[str, dict[str, dict[str, str]]] = {}
    for meeting in root:
        observation = meeting.get("observation")
        if not observation:
            continue
        channels: dict[str, dict[str, str]] = {}
        for speaker in meeting:
            channel = speaker.get("nxt_agent")
            if not channel:
                continue
            channels[channel] = {
                "role": speaker.get("role") or "",
                "global_name": speaker.get("global_name") or "",
            }
        out[observation] = channels
    return out


def read_channel_words(path: Path) -> list[TimedToken]:
    """Parse one NXT ``*.words.xml`` into timed tokens, in file order.

    Skips punctuation nodes and the ``<vocalsound>``/``<nonvocalsound>``/
    ``<comment>`` siblings (which carry no text and frequently no time at all),
    and re-attaches clitics: NXT splits ``I've`` into ``I`` + ``'ve`` while QMSum
    keeps it whole, so a leading-apostrophe token is folded into its predecessor
    and the pair's time span merged.
    """
    root = ET.parse(path).getroot()  # noqa: S314  # nosec B314 — see module note on XML trust
    out: list[TimedToken] = []
    for el in root.iter():
        if not el.tag.endswith("w") or el.get("punc") == "true":
            continue
        surface = (el.text or "").strip()
        token = normalize_token(surface)
        if not token:
            continue
        raw_start, raw_end = el.get("starttime"), el.get("endtime")
        if not raw_start or not raw_end:
            continue
        start, end = float(raw_start), float(raw_end)
        if token.startswith("'") and out:
            prev = out[-1]
            out[-1] = TimedToken(prev.token + token, prev.start, end, prev.surface + surface)
        else:
            out.append(TimedToken(token, start, end, surface))
    return out


def turn_tokens(turns: list[Turn], speaker: str) -> list[tuple[str, int, str]]:
    """Tokenise one speaker's turns into ``(normalized, turn_index, surface)``."""
    out: list[tuple[str, int, str]] = []
    for turn in turns:
        if turn.speaker != speaker:
            continue
        for raw in _MARKER_RE.sub(" ", turn.text).split():
            token = normalize_token(raw)
            if token:
                out.append((token, turn.turn_index, raw))
    return out


def _match_positions(ref: list[TimedToken], hyp: list[tuple[str, int, str]]) -> dict[int, int]:
    """Map hypothesis token position -> reference token position for exact runs."""
    matcher = difflib.SequenceMatcher(
        None, [t.token for t in ref], [t[0] for t in hyp], autojunk=False
    )
    mapping: dict[int, int] = {}
    for op, i1, i2, j1, _j2 in matcher.get_opcodes():
        if op == "equal":
            for offset in range(i2 - i1):
                mapping[j1 + offset] = i1 + offset
    return mapping


def align_turns_to_channels(
    turns: list[Turn], channel_files: dict[str, Path]
) -> tuple[int, int, int]:
    """Stamp times (and word timings) onto ``turns`` from timed reference channels.

    Args:
        turns: The corpus's turns, mutated in place.
        channel_files: QMSum speaker label -> that speaker's ``*.words.xml``.

    Returns:
        ``(turns_timed, tokens_matched, tokens_total)``. The token ratio is the
        honest measure of alignment quality; the turn count is what
        :func:`~.timings.resolve_timings` thresholds on.
    """
    spans: dict[int, list[float]] = {}
    words_by_turn: dict[int, list[Word]] = defaultdict(list)
    matched = total = 0

    for speaker, path in channel_files.items():
        if not path.exists():
            continue
        ref = read_channel_words(path)
        hyp = turn_tokens(turns, speaker)
        total += len(hyp)
        if not ref or not hyp:
            continue
        mapping = _match_positions(ref, hyp)
        matched += len(mapping)
        for hyp_pos, ref_pos in mapping.items():
            _, turn_index, surface = hyp[hyp_pos]
            timed = ref[ref_pos]
            span = spans.get(turn_index)
            if span is None:
                spans[turn_index] = [timed.start, timed.end]
            else:
                span[0] = min(span[0], timed.start)
                span[1] = max(span[1], timed.end)
            words_by_turn[turn_index].append(Word(surface, timed.start, timed.end))

    by_index = {t.turn_index: t for t in turns}
    for turn_index, (start, end) in spans.items():
        turn = by_index.get(turn_index)
        if turn is None:
            continue
        turn.start = round(start, 3)
        turn.end = round(max(end, start), 3)
        turn.words = sorted(words_by_turn[turn_index], key=lambda w: (w.start, w.end))

    return len(spans), matched, total
