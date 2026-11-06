"""Read the human decision/action summaries AMI and ICSI ship, and where each one sits in time.

Both corpora annotate a meeting three ways (Carletta et al. 2005 for AMI, Janin et al. 2003 for
ICSI, both CC BY 4.0):

* ``abstractive/<id>.abssumm.xml``: a few sentences grouped into the layers ``abstract``,
  ``actions``, ``decisions``, ``problems`` and ``progress``;
* ``extractive/<id>.summlink.xml``: links from each abstractive sentence to the dialogue acts
  that support it;
* the dialogue acts themselves, which carry (ICSI) or point at (AMI) timed words.

A gold item's *time* is the start of its earliest linked dialogue act. An item with no link, or
whose link cannot be resolved to a time, still comes back, with ``start_time=None``: callers must
count it, never drop it. ICSI has no ``actions`` layer, so asking for it yields no items rather
than zeros; use :attr:`CorpusLayout.has_actions` to tell "none annotated" from "not annotated".

The layouts differ in file names only (``dialog-act`` vs ``dialogue-acts``; AMI's dialogue acts
reference word ranges, ICSI's carry ``starttime``), so one reader serves both.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.scripts.corpus_injection.nxt import NxtElement
from app.scripts.corpus_injection.nxt import parse_nxt

_NITE = "{http://nite.sourceforge.net/}"
_NITE_ID = f"{_NITE}id"
_HREF_ID = re.compile(r"id\(([^)]+)\)")
_LAYERS = ("abstract", "actions", "decisions", "problems", "progress")
#: Layers the quality gate scores. ``problems`` and ``progress`` are parsed and counted but not
#: scored, because they are not what the summary merge rules protect.
SCORED_LAYERS = ("decisions", "actions")


@dataclass(frozen=True)
class CorpusLayout:
    """Where one corpus keeps the files :func:`read_gold_items` needs."""

    name: str
    abstractive_dir: Path
    extractive_dir: Path
    dialogue_act_dir: Path
    words_dir: Path
    dialogue_act_pattern: str  # format string over {meeting} and {channel}
    has_actions: bool


@dataclass(frozen=True)
class GoldItem:
    """One human-written sentence and where it sits in the meeting."""

    meeting_id: str
    layer: str
    sentence_id: str
    text: str
    #: Number of summlinks that point at this sentence. Zero means the annotator linked it to
    #: nothing, which is why it can never be placed.
    link_count: int
    #: Start (seconds) of the earliest linked dialogue act; ``None`` when unlinked or when no
    #: link resolved to a time.
    start_time: float | None

    @property
    def located(self) -> bool:
        return self.start_time is not None


def ami_layout(root: Path) -> CorpusLayout:
    """Layout of ``ami_public_manual_1.6.2`` (the directory holding ``abstractive/``)."""
    return CorpusLayout(
        name="ami",
        abstractive_dir=root / "abstractive",
        extractive_dir=root / "extractive",
        dialogue_act_dir=root / "dialogueActs",
        words_dir=root / "words",
        dialogue_act_pattern="{meeting}.{channel}.dialog-act.xml",
        has_actions=True,
    )


def icsi_layout(root: Path) -> CorpusLayout:
    """Layout of ``ICSI_plus_NXT`` (the directory holding ``ICSIplus/``)."""
    plus = root / "ICSIplus"
    summarization = plus / "Contributions" / "Summarization"
    return CorpusLayout(
        name="icsi",
        abstractive_dir=summarization / "abstractive",
        extractive_dir=summarization / "extractive",
        dialogue_act_dir=plus / "DialogueActs",
        words_dir=plus / "Words",
        dialogue_act_pattern="{meeting}.{channel}.dialogue-acts.xml",
        has_actions=False,
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _href_ids(href: str) -> tuple[str, ...]:
    """The ids in ``file#id(a)`` or ``file#id(a)..id(b)``, in order."""
    return tuple(_HREF_ID.findall(href))


def _href_file(href: str) -> str:
    return href.split("#", 1)[0]


@cache
def _word_table(words_path: Path) -> tuple[tuple[float | None, ...], dict[str, int]]:
    """Per-element start times of a words file in document order, plus an id -> position map."""
    starts: list[float | None] = []
    position: dict[str, int] = {}
    for el in parse_nxt(words_path).iter():
        element_id = el.get(_NITE_ID)
        if not element_id:
            continue
        start = el.get("starttime")
        position[element_id] = len(starts)
        starts.append(float(start) if start else None)
    return tuple(starts), position


def _range_start(words_path: Path, first: str, last: str) -> float | None:
    """Start of the first timed element in the inclusive word range ``first..last``."""
    if not words_path.exists():
        return None
    starts, position = _word_table(words_path)
    if first not in position or last not in position:
        return None
    lo, hi = sorted((position[first], position[last]))
    return next((start for start in starts[lo : hi + 1] if start is not None), None)


def _dialogue_act_starts(layout: CorpusLayout, meeting_id: str, channel: str) -> dict[str, float]:
    """``{dialogue act id: start}`` for one channel, resolving AMI word ranges when needed."""
    path = layout.dialogue_act_dir / layout.dialogue_act_pattern.format(
        meeting=meeting_id, channel=channel
    )
    if not path.exists():
        return {}
    starts: dict[str, float] = {}
    for act in parse_nxt(path):
        act_id = act.get(_NITE_ID)
        if not act_id:
            continue
        direct = act.get("starttime")
        if direct:
            starts[act_id] = float(direct)
            continue
        for child in act:
            if _local(child.tag) != "child":
                continue
            ids = _href_ids(child.get("href", ""))
            if not ids:
                continue
            words_path = layout.words_dir / _href_file(child.get("href", ""))
            start = _range_start(words_path, ids[0], ids[-1])
            if start is not None:
                starts[act_id] = start
                break
    return starts


def _channel_of(act_file: str, meeting_id: str) -> str:
    """Channel letter in ``<meeting>.<channel>.<kind>.xml``."""
    rest = act_file[len(meeting_id) + 1 :] if act_file.startswith(meeting_id + ".") else act_file
    return rest.split(".", 1)[0]


def _summlinks(layout: CorpusLayout, meeting_id: str) -> dict[str, list[tuple[str, str]]]:
    """``{abstractive sentence id: [(channel, dialogue act id), ...]}``."""
    path = layout.extractive_dir / f"{meeting_id}.summlink.xml"
    links: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if not path.exists():
        return links
    for link in parse_nxt(path):
        extractive: tuple[str, str] | None = None
        sentence: str | None = None
        for pointer in link:
            href = pointer.get("href", "")
            ids = _href_ids(href)
            if not ids:
                continue
            if pointer.get("role") == "extractive":
                extractive = (_channel_of(_href_file(href), meeting_id), ids[0])
            elif pointer.get("role") == "abstractive":
                sentence = ids[0]
        if extractive and sentence:
            links[sentence].append(extractive)
    return links


def _sentences(abstractive: NxtElement) -> list[tuple[str, str, str]]:
    """``(layer, sentence id, text)`` for every sentence under a known layer."""
    out: list[tuple[str, str, str]] = []
    for layer in abstractive:
        name = _local(layer.tag)
        if name not in _LAYERS:
            continue
        for sentence in layer:
            if _local(sentence.tag) != "sentence":
                continue
            text = " ".join("".join(sentence.itertext()).split())
            out.append((name, sentence.get(_NITE_ID, ""), text))
    return out


def read_gold_items(layout: CorpusLayout, meeting_id: str) -> list[GoldItem]:
    """Every abstractive sentence of one meeting with its link count and time.

    Raises:
        FileNotFoundError: the meeting has no abstractive summary under ``layout``.
    """
    path = layout.abstractive_dir / f"{meeting_id}.abssumm.xml"
    if not path.exists():
        raise FileNotFoundError(f"no abstractive summary for {meeting_id}: {path}")
    links = _summlinks(layout, meeting_id)
    act_starts: dict[str, dict[str, float]] = {}
    items: list[GoldItem] = []
    for layer, sentence_id, text in _sentences(parse_nxt(path)):
        sentence_links = links.get(sentence_id, [])
        times: list[float] = []
        for channel, act_id in sentence_links:
            if channel not in act_starts:
                act_starts[channel] = _dialogue_act_starts(layout, meeting_id, channel)
            start = act_starts[channel].get(act_id)
            if start is not None:
                times.append(start)
        items.append(
            GoldItem(
                meeting_id=meeting_id,
                layer=layer,
                sentence_id=sentence_id,
                text=text,
                link_count=len(sentence_links),
                start_time=min(times) if times else None,
            )
        )
    return items
