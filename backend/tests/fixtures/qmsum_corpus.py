"""A miniature QMSum tree in the dataset's exact on-disk format, with original text.

``test_rag_eval_harness.py`` used to read an injection manifest an operator had to create
from the 144 MB NAS download, and skipped when it was absent, which on a fresh stack was
always. This module writes the same layout from nothing, so the test seeds its own data.

Layout the adapter requires (``adapters/qmsum.py``): a directory named ``QMSum-<7-40 hex>``
holding ``data/<Domain>/all/<meeting>.json``. ``Committee`` is used because that domain gets
synthetic timings by design, so no AMI or ICSI recordings are needed.

Per-meeting schema, verified against a real ``covid_0.json``: ``topic_list``,
``general_query_list``, ``specific_query_list`` (``{query, answer, relevant_text_span}`` with
decimal-string, inclusive ``[start, end]`` turn spans) and ``meeting_transcripts``
(``{speaker, content}``). The text is original: no licensed material is copied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TREE_NAME = "QMSum-feedface00"
DOMAIN = "Committee"
MEETING_IDS = ("qmfix_0", "qmfix_1")
SPEAKERS = ("Chair Morgan", "Member Ellis", "Member Okafor", "Clerk Haldane")
TURNS_PER_MEETING = 40

# One topic per block of five turns. Each block's subject is a distinctive noun phrase so
# a query about it has exactly one block of gold turns, and BM25 can find that block.
_TOPICS = (
    ("harbour dredging contract", "the dredging schedule slipped by three weeks"),
    ("library opening hours", "evening hours would need two extra part time staff"),
    ("tram line extension", "the northern extension depends on the bridge survey"),
    ("flood barrier funding", "the barrier grant covers only the first two sections"),
    ("school meal pricing", "the price freeze ends at the close of the autumn term"),
    ("allotment waiting list", "forty households have waited more than two years"),
    ("recycling depot move", "the depot lease expires before the new site is ready"),
    ("night bus review", "ridership on the late routes fell by a fifth"),
)
_BLOCK = 5


def _turn_text(meeting: int, turn: int) -> str:
    block, slot = divmod(turn, _BLOCK)
    topic, fact = _TOPICS[(block + meeting * 3) % len(_TOPICS)]
    lines = (
        f"Moving on to the {topic}, I would like the committee to note that {fact}.",
        f"On the {topic}, my concern is cost, because {fact} and nobody has priced the delay.",
        f"I support the {topic} proposal, although {fact}, which the report understates.",
        f"The officers confirmed for the {topic} that {fact}, and a revised paper will follow.",
        f"To close the {topic} item, the committee agreed that {fact}, and recorded the decision.",
    )
    return lines[slot]


def _meeting(meeting: int) -> dict[str, Any]:
    transcripts = [
        {
            "speaker": SPEAKERS[(turn + meeting) % len(SPEAKERS)],
            "content": _turn_text(meeting, turn),
        }
        for turn in range(TURNS_PER_MEETING)
    ]
    specific = []
    for block in range(TURNS_PER_MEETING // _BLOCK):
        topic, fact = _TOPICS[(block + meeting * 3) % len(_TOPICS)]
        start = block * _BLOCK
        # Alternate between a multi-turn span and a single-turn span; the single-turn
        # ones are phrased "according to <speaker>" so the SPEAKER_ATTR carve fires.
        if block % 2 == 0:
            specific.append(
                {
                    "query": f"What did the committee decide about the {topic}?",
                    "answer": f"It agreed that {fact}.",
                    "relevant_text_span": [[str(start), str(start + _BLOCK - 1)]],
                }
            )
        else:
            turn = start + 1
            who = transcripts[turn]["speaker"]
            specific.append(
                {
                    "query": f"According to {who}, what is the concern about the {topic}?",
                    "answer": f"The cost, because {fact}.",
                    "relevant_text_span": [[str(turn), str(turn)]],
                }
            )
    return {
        "topic_list": [
            {"topic": t, "relevant_text_span": [["0", str(TURNS_PER_MEETING - 1)]]}
            for t, _ in _TOPICS[:2]
        ],
        "general_query_list": [
            {"query": "Summarize the whole meeting.", "answer": "A committee session."}
        ],
        "specific_query_list": specific,
        "meeting_transcripts": transcripts,
    }


def write_qmsum_fixture(root: Path) -> Path:
    """Write the fixture under ``root`` and return the corpus root for ``QMSumAdapter``."""
    data_dir = root / TREE_NAME / "data" / DOMAIN / "all"
    data_dir.mkdir(parents=True, exist_ok=True)
    for index, meeting_id in enumerate(MEETING_IDS):
        (data_dir / f"{meeting_id}.json").write_text(
            json.dumps(_meeting(index), indent=1), encoding="utf-8"
        )
    return root
