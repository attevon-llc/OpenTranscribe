"""Fact aspects, anchor allocation, and the uniqueness guarantee.

The whole synthetic tier rests on one invariant:

    **An anchor string appears in the corpus if and only if the generator planted it
    there, and the generator recorded where.**

Ground truth is therefore not inferred from the text — it is the write log. Anchors are
allocated from disjoint, counter-driven namespaces so collisions are impossible by
construction rather than by luck, and ``validate.py`` re-derives every gold set by brute
force over the written files so the invariant is *checked*, not merely asserted.

Anchors never appear in a query's paraphrase surface. A query names the *topic* (shared
across many of that team's meetings) and the *aspect* (shared corpus-wide), so a lexical
retriever has to discriminate rather than pattern-match a rare token. The ``verbatim``
surface, which does quote the anchor, is generated as a separate labelled subset and
reported as the easy-end control.
"""

from __future__ import annotations

from dataclasses import dataclass

from .grammar import CODE_ADJ
from .grammar import CODE_NOUN
from .grammar import FIRST_NAME
from .grammar import LAST_NAME
from .grammar import VENDOR_HEAD
from .grammar import VENDOR_TAIL
from .rng import Rng


@dataclass(frozen=True)
class Aspect:
    """One answerable property of a topic.

    Attributes:
        name: Aspect id, recorded in ``facts.jsonl`` for provenance.
        anchor_kind: Which allocator namespace supplies the answer string.
        query_phrase: Anchor-free NOUN PHRASE naming this aspect. It has to be a noun
            phrase because the query frames read "what was {query_phrase} for
            {topic}?" and list several of them in one multi-file question; an
            interrogative here produced "what was who ended up owning it for X?".
        interactive: Render templates for the conversational register.
        formal: Render templates for the parliamentary register.
    """

    name: str
    anchor_kind: str
    query_phrase: str
    interactive: tuple[str, ...]
    formal: tuple[str, ...]


ASPECTS: tuple[Aspect, ...] = (
    Aspect(
        "throughput",
        "rps",
        "the peak throughput we measured",
        (
            "We finally got a clean run on {topic} — it topped out at {anchor}.",
            "The load test for {topic} came back at {anchor}, which is better than I expected.",
            "So {topic} sustained {anchor} before anything started queueing.",
        ),
        (
            "The measured peak for {topic} was {anchor}, which the working group considers "
            "sufficient headroom for the coming period.",
        ),
    ),
    Aspect(
        "latency_budget",
        "ms",
        "the latency budget we agreed",
        (
            "We agreed the budget for {topic} is {anchor} and we hold ourselves to that.",
            "For {topic} the ceiling is {anchor}. Anything past that is a regression.",
            "{anchor} is the number we settled on for {topic}, end to end.",
        ),
        ("The board agreed a service budget of {anchor} for {topic}, to be reviewed annually.",),
    ),
    Aspect(
        "owner",
        "person",
        "the person who ended up owning it",
        (
            "{anchor} is picking up {topic} from here — they've got the most context.",
            "Ownership of {topic} moves to {anchor} as of this week.",
            "We handed {topic} to {anchor}, so route questions there.",
        ),
        (
            "Responsibility for {topic} has been assigned to {anchor}, who will report to this "
            "board at the next session.",
        ),
    ),
    Aspect(
        "vendor",
        "vendor",
        "the supplier we selected",
        (
            "We went with {anchor} for {topic} in the end. Their integration story was cleaner.",
            "{anchor} won the {topic} evaluation, mostly on operational fit.",
            "Decision on {topic}: {anchor}. The other two didn't clear the security review.",
        ),
        (
            "Following evaluation, the recommended supplier for {topic} is {anchor}, and the "
            "board is asked to endorse that selection.",
        ),
    ),
    Aspect(
        "deadline",
        "milestone",
        "the milestone we committed to",
        (
            "We committed {topic} to {anchor}. That's firm now.",
            "{topic} lands in {anchor}, assuming nothing else jumps the queue.",
            "The date for {topic} is {anchor} — I've updated the plan.",
        ),
        ("Delivery of {topic} is committed to {anchor} and has been recorded in the plan.",),
    ),
    Aspect(
        "cost",
        "amount",
        "the cost it was going to carry",
        (
            "The number for {topic} came in at {anchor} for the year.",
            "{topic} is {anchor} annualised, which is under what we budgeted.",
            "Finance came back: {anchor} for {topic}, excluding the migration effort.",
        ),
        (
            "The estimated annual cost of {topic} is {anchor}, which falls within the envelope "
            "previously approved.",
        ),
    ),
    Aspect(
        "ticket",
        "ticket",
        "the tracking ticket that was raised",
        (
            "I raised {anchor} to track {topic} so it doesn't get lost again.",
            "{topic} is tracked under {anchor} now.",
            "There's a ticket for {topic} — {anchor} — with the full write-up attached.",
        ),
        ("A tracking item, {anchor}, has been opened in respect of {topic}.",),
    ),
    Aspect(
        "version",
        "version",
        "the release it was scheduled into",
        (
            "{topic} is going out in {anchor}, not the one before it.",
            "We slipped {topic} to {anchor} to get a full soak cycle.",
            "Target release for {topic} is {anchor}.",
        ),
        ("The change relating to {topic} is scheduled for release {anchor}.",),
    ),
    Aspect(
        "capacity",
        "regions",
        "the share of the estate it covers",
        (
            "Right now {topic} covers {anchor}, and the rest are queued behind it.",
            "{topic} is live in {anchor} — the remainder need the migration first.",
            "Coverage for {topic} is {anchor} today.",
        ),
        ("Current coverage for {topic} extends to {anchor}.",),
    ),
    Aspect(
        "rollback",
        "duration",
        "the length of the rollback window",
        (
            "The rollback window on {topic} is {anchor}, which is tighter than I'd like.",
            "We can back {topic} out within {anchor} if it misbehaves.",
            "{anchor} is the recovery window we designed {topic} around.",
        ),
        ("The recovery window applicable to {topic} is {anchor}.",),
    ),
)

ASPECTS_BY_NAME = {a.name: a for a in ASPECTS}


class AnchorAllocator:
    """Hands out globally unique anchor strings, one namespace per anchor kind.

    Uniqueness is structural: every kind draws from a monotonically increasing counter
    (optionally combined with a without-replacement name draw), so two anchors can never
    collide and no anchor can coincide with the generic vocabulary used by ``grammar.py``
    templates. ``used`` is the audit trail the validator checks against.
    """

    def __init__(self, rng: Rng) -> None:
        """Initialise the counters and the shuffled reserved name pools."""
        self._counters: dict[str, int] = {}
        # A middle initial extends the pool 9x without a bigger name list, which matters
        # at the 20k/50k rungs of the scale ladder (one roster entry per ~4 meetings).
        self._people = rng.shuffled(
            [f"{f} {n}" for f in FIRST_NAME for n in LAST_NAME]
            + [f"{f} {i}. {n}" for i in "ABCDEFGH" for f in FIRST_NAME for n in LAST_NAME]
        )
        self._vendors = rng.shuffled([f"{h} {t}" for h in VENDOR_HEAD for t in VENDOR_TAIL])
        self._codes = rng.shuffled(
            [f"{a.capitalize()} {n.capitalize()}" for a in CODE_ADJ for n in CODE_NOUN]
        )
        self.used: list[str] = []

    def _next(self, kind: str) -> int:
        n = self._counters.get(kind, 0)
        self._counters[kind] = n + 1
        return n

    def _take(self, pool: list[str], kind: str) -> str:
        idx = self._next(kind)
        if idx >= len(pool):
            raise RuntimeError(f"exhausted the {kind} name pool at {len(pool)} entries")
        return pool[idx]

    def allocate(self, kind: str) -> str:
        """Return a fresh, globally unique anchor string of the given kind."""
        n = self._next(kind)
        if kind == "rps":
            value = f"{10_000 + n * 137:,} requests per second"
        elif kind == "ms":
            value = f"{180 + n * 7} milliseconds"
        elif kind == "person":
            value = self._take(self._people, "person-pool")
        elif kind == "vendor":
            value = self._take(self._vendors, "vendor-pool")
        elif kind == "milestone":
            value = f"milestone M-{4100 + n}"
        elif kind == "amount":
            value = f"${120_000 + n * 311:,}"
        elif kind == "ticket":
            value = f"OPS-{50_000 + n}"
        elif kind == "version":
            # n = (n//400)*400 + ((n//20)%20)*20 + n%20, so the triple is a bijection.
            value = f"v{4 + n // 400}.{(n // 20) % 20}.{n % 20}"
        elif kind == "regions":
            value = f"{2 + n} of our {900 + n} sites"
        elif kind == "duration":
            value = f"{11 + n} minutes"
        elif kind == "codename":
            value = self._take(self._codes, "codename-pool")
        else:
            raise ValueError(f"unknown anchor kind: {kind}")
        self.used.append(value)
        return value
