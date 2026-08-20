"""Unanswerable-entity probes (#463): ~30 deterministic absent-entity queries.

**No relevance metric can see this failure mode.** A system that retrieves nothing
relevant and correctly declines scores exactly the same, on nDCG/recall/ROUGE alike,
as a system that fabricates a confident answer about an entity that never appears in
the corpus — both submitted something (or nothing) and neither is being checked
against what the RIGHT behaviour was. ``false_answer_rate`` is that check: for each
probe, did the system decline (or say "not found"/"no information"), or did it answer
as though the entity were real?

Entities below are deliberately **implausible as organic transcript content, not
merely unlikely** — invented proper nouns built from no real dictionary-word roots
(``Zorblatt``, ``Kwenzalotl``, ...), so a probe never accidentally becomes answerable
because the corpus happens to discuss something similarly named. This is a stronger
guarantee than picking obscure-but-real names, which QMSum's real committee/business
transcripts could plausibly mention by coincidence.

Deterministic in ``(file_uuids, seed)`` — same SplitMix64 generator the rest of the
synthetic tier uses (:mod:`tests.eval.synthetic.rng`), so a probe set regenerates
byte-for-byte, matching every other generator in this package's own claim.

**No LLM is involved at any point** (D6) — this module only plants probe TEXT; running
one against a system and checking the reply is a separate step (the caller's).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tests.eval.synthetic.rng import Rng
from tests.eval.synthetic.rng import derive_seed

#: Invented proper nouns with no real dictionary-word roots — see module docstring
#: for why this is stronger than picking obscure-but-real names.
ABSENT_ENTITIES: tuple[str, ...] = (
    "Zorblatt Industries",
    "Kwenzalotl Corporation",
    "Vindrossa Analytics",
    "Thalorix Systems",
    "Bexnavia Group",
    "Quilmerak Foundation",
    "Yttrafen Holdings",
    "Ombrellix Partners",
    "Snargolt Ventures",
    "Corvantheon Labs",
    "Plexivane Consulting",
    "Drazmurthy Council",
    "Wexnaria Institute",
    "Bloqastrel Networks",
    "Fenrivosk Alliance",
    "Grommelate Union",
    "Talvistrix Media",
    "Ozenquith Trust",
    "Marlithane Cooperative",
    "Vexholt Enterprises",
)

_TEMPLATES: tuple[str, ...] = (
    "What did {entity} say about the budget?",
    "How many times was {entity} mentioned?",
    "Who from {entity} attended the meeting?",
    "What deadline did {entity} agree to?",
    "Summarize {entity}'s role in the project.",
    "What concerns did {entity} raise?",
)

DEFAULT_PROBE_COUNT = 30

#: Phrases that read as a genuine decline / "not found" rather than a fabricated
#: answer. Deliberately conservative (module docstring: :func:`is_false_answer`).
DECLINE_MARKERS: tuple[str, ...] = (
    "not mentioned",
    "no mention",
    "not found",
    "not discussed",
    "no information",
    "don't have",
    "do not have",
    "no record",
    "not in the",
    "cannot find",
    "can't find",
    "couldn't find",
    "could not find",
    "unable to find",
    "not present",
    "isn't mentioned",
    "is not mentioned",
    "not addressed",
    "not referenced",
    "not covered",
    "no evidence",
    "not appear",
    "does not appear",
    "doesn't appear",
    "not include",
    "not aware",
)


@dataclass(frozen=True)
class UnanswerableProbe:
    """One absent-entity probe: a question about something that isn't in the corpus."""

    probe_id: str
    text: str
    entity: str
    file_uuid: str


def plant_unanswerable_probes(
    file_uuids: list[str], *, count: int = DEFAULT_PROBE_COUNT, seed: int = 0
) -> list[UnanswerableProbe]:
    """Plant ``count`` deterministic absent-entity probes across ``file_uuids``.

    Args:
        file_uuids: recordings to (nominally) scope each probe to — the probe's
            entity is absent from ALL of them, so which file is attached is
            cosmetic, not load-bearing; it exists so a probe can be driven the
            same way a real query is (with a scope, a text, an id).
        count: how many probes to plant. Defaults to 30 (issue #463).
        seed: selects entity/template/file assignment, independent of any other
            generator in this package.

    Returns:
        ``count`` probes, or ``[]`` if ``file_uuids`` is empty (nothing to scope
        a probe to).
    """
    if not file_uuids:
        return []
    probes: list[UnanswerableProbe] = []
    for index in range(count):
        entity = ABSENT_ENTITIES[index % len(ABSENT_ENTITIES)]
        template = _TEMPLATES[index % len(_TEMPLATES)]
        rng = Rng(derive_seed("unanswerable", seed, index))
        file_uuid = rng.choice(file_uuids)
        probes.append(
            UnanswerableProbe(
                probe_id=f"unanswerable:{seed}:{index:03d}",
                text=template.format(entity=entity),
                entity=entity,
                file_uuid=file_uuid,
            )
        )
    return probes


def is_false_answer(submitted: str | None) -> bool:
    """Whether ``submitted`` reads as a fabricated (confident) answer, not a decline.

    A conservative heuristic, not a semantic judge: it can UNDERCOUNT false answers
    (a genuine decline phrased outside :data:`DECLINE_MARKERS` reads as false) but
    never overcounts an explicit decline as false, because every marker it checks
    for is itself an admission of absence, never a hedge that could also introduce
    a fabricated answer in the same sentence.

    Args:
        submitted: the system's reply, or ``None``/blank for an explicit decline.

    Returns:
        ``True`` if this counts as a false (fabricated) answer.
    """
    if submitted is None or not submitted.strip():
        return False
    lowered = submitted.casefold()
    return not any(marker in lowered for marker in DECLINE_MARKERS)


def false_answer_rate(
    probes: list[UnanswerableProbe], submitted: Mapping[str, str | None]
) -> float:
    """Fraction of ``probes`` the system answered falsely (fabricated a response).

    A probe id absent from ``submitted`` is treated as a decline (``None``), not
    skipped — the denominator is always ``len(probes)``, never narrowed by what a
    caller happened to submit.

    Raises:
        ValueError: ``probes`` is empty.
    """
    if not probes:
        raise ValueError("false_answer_rate: no probes to score")
    false_count = sum(1 for probe in probes if is_false_answer(submitted.get(probe.probe_id)))
    return false_count / len(probes)
