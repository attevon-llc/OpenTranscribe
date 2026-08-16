"""Vocabulary pools, turn templates and register profiles.

Everything a synthetic meeting is made of is in this module, and **none of it is copied
from a third-party corpus** — that is a licence requirement, not a stylistic one: text
lifted from MeetingBank (CC BY-NC-ND) would make the generated corpus non-publishable.

The register profiles at the bottom ARE fitted to real data. Their numbers were measured
on 2026-08-12 from QMSum's own transcripts on the NAS (232 meetings; ``Product`` = AMI,
``Academic`` = ICSI, ``Committee`` = parliamentary), not recalled from a paper. See
``.rag-403/synthetic-tier-design.md`` §3 for the measuring script and the full table.
"""

from __future__ import annotations

from dataclasses import dataclass


def _w(text: str) -> tuple[str, ...]:
    """Split a whitespace-packed pool into a tuple (keeps this file readable)."""
    return tuple(text.split())


# --- Name pools. Products of two pools, so uniqueness is combinatorial, not curated. ---

CODE_ADJ = _w(
    "amber azure basalt bramble cedar cinder cobalt copper cypress dawn ember fallow flint "
    "garnet granite harbor hazel indigo ironwood jasper juniper kestrel larch lichen marble "
    "meridian mistral nimbus obsidian onyx pewter quartz redwood rowan russet sable sandstone "
    "sequoia sienna slate solstice sorrel spruce sumac tamarack thistle topaz umber verdant "
    "willow zephyr"
)
CODE_NOUN = _w(
    "anchor arbor arcade atlas beacon bellows bridge canopy cascade circuit compass conduit "
    "cornice crossing ferry foundry gantry harbour hearth keystone lantern ledger lighthouse "
    "lintel meridian mooring orchard parapet pilot quarry rampart ridge rookery sextant shoal "
    "signal spindle stanchion terrace threshold trellis turnstile vantage viaduct waypoint "
    "wharf windlass yardarm"
)
COMPONENT_HEAD = _w(
    "ingest export policy billing routing scheduling indexing archival telemetry provisioning "
    "reconciliation notification retention onboarding settlement enrichment throttling caching "
    "replication compaction"
)
COMPONENT_TAIL = _w(
    "gateway pipeline service worker planner broker registry collector adapter dispatcher "
    "sidecar controller store queue shim"
)
#: Third factor in the component name, so the pool scales past a few hundred teams
#: without ever reusing a phrase (20 x 15 x 12 = 3,600 disjoint component names).
COMPONENT_QUAL = _w(
    "primary regional batch edge tenant-scoped legacy federated inline shadow bulk "
    "low-latency cross-region"
)
METRIC_NAME = _w(
    "p99-latency cold-start-time queue-depth error-budget cache-hit-rate ingest-lag "
    "index-freshness retry-rate throughput fan-out-cost storage-footprint tail-latency "
    "handoff-time saturation backlog-age"
)
VENDOR_HEAD = _w(
    "Brightmoor Calderwood Dunmore Eastbrook Fernhill Glenmere Halvard Ironmount Jerrick "
    "Kelmscott Larkfield Marchmont Northwind Oakhaven Pemberly Quillon Ravensworth Stanmore "
    "Thornbury Uplands Vexley Wrenfield Yardley Zellwood"
)
VENDOR_TAIL = _w("Systems Analytics Networks Labs Dynamics Logistics Technologies Partners")
FIRST_NAME = _w(
    "Adaeze Alina Amara Anders Anika Beatriz Callum Camille Cyrus Dara Delia Dmitri Elif Esme "
    "Farid Fenella Gideon Halle Hiroshi Imani Ines Jonas Juno Kaveh Keiko Lena Lucian Maeve "
    "Marek Nadia Nils Odile Omar Petra Quentin Rania Rosa Sanjay Sigrid Talia Teo Ulrike Vikram "
    "Wren Xiomara Yusuf Zaid Zora"
)
LAST_NAME = _w(
    "Abara Ashcroft Belanger Castellan Dorsey Ekhart Fennimore Grieve Halloran Ingersoll "
    "Jandreau Kowalczyk Lindqvist Mbeki Novak Okonjo Prentiss Quillan Rasmussen Sandoval "
    "Thorne Underhill Vasquez Whitfield Yarrow Zabinski"
)
ROLE = _w(
    "engineer analyst lead architect manager designer researcher coordinator specialist "
    "reviewer strategist operator"
)
TEAM_DOMAIN = _w(
    "platform reliability data-services security payments identity search mobile growth "
    "infrastructure integrations analytics compliance tooling billing partnerships "
    "workplace-tech logistics support-engineering media-pipeline forecasting"
)
DIVISION_NAME = _w(
    "Operations Engineering Commercial Research Corporate-Services Field-Delivery "
    "Product-Group Risk-and-Assurance"
)
SERIES_KIND = (
    ("weekly sync", "interactive"),
    ("design review", "interactive"),
    ("incident review", "interactive"),
    ("sprint retrospective", "interactive"),
    ("backlog triage", "interactive"),
    ("architecture forum", "formal"),
    ("quarterly planning", "formal"),
    ("vendor review board", "formal"),
    ("change advisory board", "formal"),
    ("steering committee", "formal"),
)

# --- Disfluency inventory. Rates live in the register profiles. ---

FILLERS = _w("um uh er hmm mm")
HEDGES = ("you know", "I mean", "sort of", "kind of", "I guess", "more or less")
BACKCHANNEL = (
    "Mm-hmm.",
    "Right.",
    "Yeah.",
    "Okay.",
    "Sure.",
    "Got it.",
    "Yeah, exactly.",
    "Mm, okay.",
    "Right, yeah.",
    "Understood.",
)
FALSE_START = ("So the — sorry, ", "I think we — well, ", "We should — actually, ", "Can we — ")

# --- Turn templates. ``{slot}`` names are filled by render.py. ---
# Templates deliberately reuse the same *process* vocabulary across every team; only the
# slot fillers are team-specific. That is what stops the corpus from being trivially
# separable by topic words alone, and it mirrors how real org transcripts read.

INTERACTIVE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "agenda_open": (
        "Alright, {series} for {team}. First up is {topic}, then the {component}.",
        "Okay — this is the {series}. Agenda is {topic} and whatever's left on the {component}.",
        "Let's start the {series} for {team}. {person} wanted to walk through {topic} first.",
    ),
    "status": (
        "So on {topic}, we're roughly where we expected. The {component} is behaving.",
        "Quick status: {topic} moved forward this week, mostly on the {component} side.",
        "Not much change on {topic}. The {component} work is still in review.",
        "{topic} is unblocked now. {person} picked up the {component} piece.",
    ),
    "question": (
        "What's the current story on {topic}?",
        "Can someone remind me where we landed on the {component}?",
        "Do we know why the {metric} moved on {topic}?",
        "Is the {component} still the bottleneck for {topic}?",
    ),
    "answer": (
        "It's mostly the {component}. Once that settles, {topic} follows.",
        "Yeah — {person} looked at it. The {metric} is the thing driving it.",
        "Partly. The {component} is fine now, it's the downstream side of {topic}.",
    ),
    "concern": (
        "I'm a bit nervous about {topic}, honestly. The {component} has bitten us before.",
        "My worry is the {metric}. If that drifts, {topic} stalls again.",
        "We keep saying the {component} is fine and then {topic} slips.",
    ),
    "agreement": (
        "Yeah, that matches what I saw.",
        "Agreed — that's the right read on {topic}.",
        "Same conclusion here.",
        "That's fair.",
    ),
    "action": (
        "Action item: {person} to write up the {component} change before next session.",
        "Let's put {person} down for the {topic} follow-up.",
        "I'll take the {component} ticket and report back on {topic}.",
    ),
    "digression": (
        "Unrelated, but the room booking for this slot keeps moving.",
        "Side note — the dashboards were down for an hour this morning.",
        "Sorry, my connection dropped. Can you repeat the last part?",
    ),
    "scheduling": (
        "Can we push the rest of {topic} to next session? We're nearly out of time.",
        "Let's book a follow-up on the {component} with {person} and whoever else is close to it.",
    ),
    "close": (
        "Okay, that's the {series} done. Thanks everyone.",
        "Good session. We'll pick {topic} back up at the next {series}.",
    ),
}

FORMAL_TEMPLATES: dict[str, tuple[str, ...]] = {
    "agenda_open": (
        "Good morning, and welcome to this session of the {series} for {team}. The agenda "
        "covers {topic}, the current state of the {component}, and any items carried over "
        "from the previous meeting. I would ask members to keep interventions brief so that "
        "we reach the substantive items.",
        "This session of the {series} for {team} is now open. Before {topic}, I should note "
        "that the papers were circulated on time and that the {component} update is appended.",
    ),
    "status": (
        "Turning to {topic}. The position is broadly as reported previously: the {component} has "
        "progressed, the {metric} remains the principal indicator we are tracking, and no further "
        "escalation is proposed at this stage. {person} has been coordinating the workstream.",
        "On {topic}, the working group met twice since the last session. Their assessment is that "
        "the {component} is now stable enough to proceed, subject to the {metric} holding at its "
        "current level through the next reporting period.",
        "Members will recall that {topic} was paused pending the review of the {component}. That "
        "review has now concluded and the recommendation before the board is to resume.",
        "The written update on {topic} was circulated on Friday. In summary, the {component} is "
        "no longer on the critical path and the {metric} has been re-baselined accordingly.",
        "I can report that {topic} is proceeding, though not at the pace originally set out. The "
        "constraint continues to be the {component}, and {person} has been asked to advise.",
    ),
    "question": (
        "May I ask the sponsor to clarify the position on {topic}, and in particular what "
        "assurance the board has that the {component} will not require a further extension?",
        "Could the sponsor set out, for the record, how the {metric} on {topic} is being "
        "monitored between sessions?",
        "My question concerns sequencing. Is the {component} a prerequisite for {topic}, or can "
        "the two proceed in parallel?",
    ),
    "answer": (
        "Thank you for the question. The short answer is that {topic} is proceeding to plan. The "
        "{component} was the principal dependency and that has now been resolved; the {metric} is "
        "the measure we would expect to move first if that assessment proved optimistic.",
        "I am grateful to the member for raising it. The position on {topic} is that the "
        "{component} is being handled under the existing delegation, and {person} reports on the "
        "{metric} at each fortnightly checkpoint.",
        "The candid answer is that we do not yet know. What I can tell the board is that {topic} "
        "has not slipped further, and that the {component} is being tracked separately.",
    ),
    "concern": (
        "I would register a concern about {topic}. The board has heard similar assurances about "
        "the {component} before, and the {metric} has not yet demonstrated a sustained improvement. "
        "I would prefer a firmer commitment before we close the item.",
        "I am not persuaded. If the {component} were genuinely resolved, the {metric} on {topic} "
        "would have moved by now, and it has not.",
        "My reservation is one of capacity rather than intent. {person} cannot reasonably carry "
        "{topic} and the {component} at the same time.",
    ),
    "agreement": (
        "I support that recommendation.",
        "That is consistent with the paper circulated ahead of this session, and I am content.",
        "I have nothing to add; the position on {topic} seems sound.",
        "Noted, and I would echo the point about the {component}.",
    ),
    "action": (
        "The action is recorded: {person} to bring a written update on the {component} to the next "
        "session, covering both {topic} and the associated {metric}.",
        "The board directs that {topic} be reviewed again once the {component} has completed, and "
        "that {person} circulate a note in advance.",
        "I will minute an action for {person} to reconcile the {metric} figures for {topic} before "
        "the next reporting cycle.",
    ),
    "digression": (
        "Before we move on, a procedural point: the minutes of the previous session were "
        "circulated late and members may not have had time to review them.",
        "For the record, two members have given apologies and are not present for this item.",
        "I should declare an interest at this point, as it touches the {component}.",
    ),
    "scheduling": (
        "In the interest of time I propose we carry the remainder of {topic} to the next session "
        "and prioritise it on that agenda.",
        "Given the hour, I suggest we take {topic} as read and return to it under any other "
        "business if members wish.",
    ),
    "close": (
        "That concludes the agenda of the {series}. Thank you all; the minutes will follow.",
        "There being no further business, I close this session of the {series} for {team}.",
    ),
}

#: Optional trailing clauses, attached to substantive turns so the number of distinct
#: turn surfaces is combinatorial rather than equal to the template count. Without these
#: the formal register repeated the same sentence three times in fourteen turns, which
#: would have inflated the corpus's own near-duplicate rate for no good reason.
CONTINUATIONS = {
    "interactive": (
        "The {metric} is the thing I'd watch.",
        "{person} has the details if anyone wants them.",
        "It's not urgent, but it's not nothing either.",
        "We've got maybe two weeks of slack there.",
        "I'll drop the notes in the channel afterwards.",
        "Same shape as the {component} problem from last month.",
        "Nobody's blocked on it right now.",
        "That's assuming nothing else lands on us this week.",
        "I'd rather not re-litigate that one today.",
        "Happy to be wrong about that.",
        "The numbers are in the deck if you want them.",
        "We should probably write that down somewhere.",
    ),
    "formal": (
        "The relevant figures are set out in annex B of the circulated paper.",
        "I would not wish the board to take that as a commitment at this stage.",
        "This is consistent with the assurance given at the previous session.",
        "{person} is available to answer detailed questions on the {component}.",
        "The risk register has been updated to reflect that position.",
        "No additional resource is being sought under this item.",
        "I would ask that this be reflected in the minutes.",
        "The timetable remains as previously agreed.",
    ),
}

TEMPLATES_BY_REGISTER = {"interactive": INTERACTIVE_TEMPLATES, "formal": FORMAL_TEMPLATES}

#: Non-fact acts, with the sampling weight used to build a meeting's turn sequence.
FILLER_ACT_WEIGHTS = (
    ("status", 24),
    ("question", 16),
    ("answer", 16),
    ("concern", 9),
    ("agreement", 10),
    ("action", 9),
    ("digression", 6),
    ("scheduling", 5),
)


@dataclass(frozen=True)
class Register:
    """A speaking register, fitted to a measured QMSum domain.

    Attributes:
        name: Register id used in the corpus config and per-meeting metadata.
        fitted_to: The QMSum domain whose measured statistics these values reproduce.
        words_median: Median words per meeting.
        words_sigma: Log-scale sigma for the meeting-length lognormal.
        speakers_low: Minimum attendee count.
        speakers_high: Maximum attendee count.
        short_turn_fraction: Fraction of turns that are backchannels (<= 3 words).
        filler_rate: Target core-filler tokens (um/uh/er/hmm/mm) as a fraction of words.
        hedge_rate: Probability a substantive turn gains a hedge phrase.
        false_start_rate: Probability a substantive turn opens with a repair.
        words_per_second: Speaking rate used to synthesise timestamps.
    """

    name: str
    fitted_to: str
    words_median: float
    words_sigma: float
    speakers_low: int
    speakers_high: int
    short_turn_fraction: float
    filler_rate: float
    hedge_rate: float
    false_start_rate: float
    words_per_second: float


#: Measured on QMSum 2026-08-12. ``interactive`` blends the AMI (Product) and ICSI
#: (Academic) domains: 4-9 speakers, 9.7-14.5 words/turn, 4.1-4.9% core fillers,
#: 41-47% short turns. ``formal`` reproduces the Committee domain: 6-11 speakers,
#: 69 words/turn, 0% fillers (parliamentary transcripts are cleaned), 8% short turns.
REGISTERS: dict[str, Register] = {
    "interactive": Register(
        name="interactive",
        fitted_to="QMSum Product (AMI) + Academic (ICSI)",
        words_median=7000.0,
        words_sigma=0.45,
        speakers_low=4,
        speakers_high=9,
        short_turn_fraction=0.44,
        filler_rate=0.045,
        hedge_rate=0.22,
        false_start_rate=0.07,
        words_per_second=2.6,
    ),
    "formal": Register(
        name="formal",
        fitted_to="QMSum Committee (Welsh/Canadian parliamentary)",
        words_median=13000.0,
        words_sigma=0.35,
        speakers_low=7,
        speakers_high=12,
        short_turn_fraction=0.09,
        filler_rate=0.001,
        hedge_rate=0.03,
        false_start_rate=0.0,
        words_per_second=2.3,
    ),
}
