"""Turn rendering: acts -> speaker-attributed, disfluent, timestamped dialogue.

Output shape deliberately mirrors QMSum's ``meeting_transcripts`` (a list of
``{speaker, content}``) with timestamps added, so the Stage-1 adapter that maps gold turn
ranges onto whatever chunks the indexer produced works unchanged for both corpora. Gold
spans are emitted in QMSum's own ``[start_turn, end_turn]`` inclusive-range convention
rather than a second, incompatible one.

A planted fact renders as a **two-turn exchange** — someone asks, someone answers with the
anchor — which is both how meetings actually work and where the graded relevance comes
from: the answer turn is the target, the setup turn is context.
"""

from __future__ import annotations

from . import grammar
from .grammar import REGISTERS
from .grammar import Register
from .org import Org
from .org import Session
from .rng import Rng
from .rng import derive_seed

MARKER_TEMPLATES = (
    "We also touched on {phrase} briefly — nothing to decide yet.",
    "{phrase} came up again. I'll keep it on the standing list.",
    "Someone asked about {phrase}, so I said we'd come back to it.",
    "Last item before we close: {phrase} is still open.",
)
EVENT_TEMPLATES = (
    "We're deferring {phrase} to the next session again.",
    "Same as last time — {phrase} gets pushed out.",
    "I'm going to defer {phrase} rather than rush a decision.",
)
#: Acts long enough that a trailing clause reads naturally. Backchannel-adjacent acts
#: ("agreement") are excluded so the measured short-turn fraction stays on its fitted target.
_EXTENDABLE_ACTS = frozenset({"status", "question", "answer", "concern", "action", "scheduling"})

SETUP_TEMPLATES = (
    "Before we move on — where did we land on {topic}?",
    "Can we nail down {topic} while everyone's here?",
    "One more on {topic}: do we have a number yet?",
    "What's the answer on {topic}? I keep getting asked.",
)


def _fill(template: str, session: Session, org: Org, rng: Rng, names: list[str]) -> str:
    """Fill a template's slots from the session's team vocabulary."""
    team = org.teams[session.team_id]
    return template.format(
        topic=rng.choice(session.agenda if session.agenda else team.topics),
        component=rng.choice(team.components),
        metric=rng.choice(team.metrics),
        person=rng.choice(names),
        series=session.series_kind,
        team=team.label,
    )


#: Stand-in for an anchor while disfluency is applied. It is a single whitespace-delimited
#: token, so no filler can be inserted *inside* an anchor and no false-start repair can
#: lowercase its first letter. Without this, ``_disfluent`` split multi-word anchors like
#: "2 of our 900 sites" and the ground-truth validator caught it (V1) on the first run.
_ANCHOR_SENTINEL = "\x00anchor\x00"


def _render_anchored(template: str, anchor: str, reg: Register, rng: Rng, **slots: str) -> str:
    """Fill a template, apply disfluency, then substitute the anchor verbatim."""
    text = template.format(anchor=_ANCHOR_SENTINEL, **slots)
    return _disfluent(text, reg, rng).replace(_ANCHOR_SENTINEL, anchor)


def _disfluent(text: str, reg: Register, rng: Rng) -> str:
    """Apply the register's filler, hedge and false-start rates to one turn."""
    if reg.false_start_rate and rng.chance(reg.false_start_rate):
        text = rng.choice(grammar.FALSE_START) + text[0].lower() + text[1:]
    words = text.split()
    if reg.filler_rate > 0:
        expected = len(words) * reg.filler_rate / max(1e-9, 1 - reg.filler_rate)
        n_fill = int(expected) + (1 if rng.random() < expected - int(expected) else 0)
        for _ in range(n_fill):
            words.insert(rng.randint(0, len(words)), rng.choice(grammar.FILLERS))
    if reg.hedge_rate and rng.chance(reg.hedge_rate):
        words.insert(rng.randint(0, len(words)), rng.choice(grammar.HEDGES))
    return " ".join(words)


def _backchannel_probability(target_fraction: float) -> float:
    """Convert a target short-turn *fraction* into a per-substantive-turn insert rate.

    A backchannel is emitted after a substantive turn with probability p, so the realised
    fraction is p/(1+p), not p. Configuring p directly undershot the fitted 0.44 by 11
    points; this inverts the relation so the profile's number is the number you measure.
    """
    return target_fraction / max(1e-9, 1.0 - target_fraction)


def _weighted_act(rng: Rng) -> str:
    """Sample a filler act from :data:`grammar.FILLER_ACT_WEIGHTS`."""
    total = sum(w for _, w in grammar.FILLER_ACT_WEIGHTS)
    pick = rng.randint(1, total)
    for act, weight in grammar.FILLER_ACT_WEIGHTS:
        pick -= weight
        if pick <= 0:
            return act
    return grammar.FILLER_ACT_WEIGHTS[0][0]


def _expand_plants(session: Session, rng: Rng) -> list[dict]:
    """Expand each plant into the act entries it renders as, in stable order."""
    acts: list[dict] = []
    for plant in session.plants:
        if plant["kind"] == "fact":
            acts.append({"act": "fact", "plant": plant})
        elif plant["kind"] == "marker":
            acts.append({"act": "marker", "plant": plant})
        else:
            for _ in range(int(plant["repeats"])):
                acts.append({"act": "event", "plant": plant})
    return rng.shuffled(acts)


def render_session(session: Session, org: Org, config: dict) -> tuple[dict, list[dict]]:
    """Render one session to a meeting record plus the placement log for its plants.

    Args:
        session: The planned session (with ``plants`` already attached).
        org: The organisation, for team vocabulary and rosters.
        config: The corpus config; only ``seed`` and ``corpus_id`` are read.

    Returns:
        ``(meeting, placements)``. ``placements`` entries carry ``fact_id``/``query_id``
        and the turn indices the plant occupies, which is what becomes the gold span.
    """
    rng = Rng(derive_seed(config["seed"], "render", session.meeting_key))
    reg = REGISTERS[session.register]
    team = org.teams[session.team_id]
    people = [p for p in team.roster if p.person_id in session.attendees]
    names = [p.name for p in people]
    chair = people[0]
    templates = grammar.TEMPLATES_BY_REGISTER[session.register]
    target_words = int(rng.clamped_lognormal(reg.words_median, reg.words_sigma, 700.0, 45000.0))

    plant_acts = _expand_plants(session, rng)
    turns: list[dict] = []
    placements: list[dict] = []
    words_so_far = 0

    def emit(speaker_name: str, text: str) -> int:
        nonlocal words_so_far
        turns.append({"speaker": speaker_name, "content": text})
        words_so_far += len(text.split())
        return len(turns) - 1

    emit(chair.name, _fill(rng.choice(templates["agenda_open"]), session, org, rng, names))
    # Reserve roughly a fifth of the budget so plants land inside the body, never after
    # the closing turn: a gold span in the wrap-up would be unrealistically easy to find.
    plant_at = _plant_positions(len(plant_acts), target_words, rng)

    while words_so_far < target_words or plant_at:
        if plant_at and words_so_far >= plant_at[0]:
            plant_at.pop(0)
            entry = plant_acts.pop(0) if plant_acts else None
            if entry is not None:
                placements.append(_emit_plant(entry, emit, session, org, rng, names, reg))
                continue
        act = _weighted_act(rng)
        speaker = rng.choice(people)
        parts = [_fill(rng.choice(templates[act]), session, org, rng, names)]
        if act in _EXTENDABLE_ACTS:
            pool = grammar.CONTINUATIONS[session.register]
            extra = (
                rng.randint(3, 5)
                if session.register == "formal"
                else (1 if rng.chance(0.35) else 0)
            )
            parts += [_fill(rng.choice(pool), session, org, rng, names) for _ in range(extra)]
        emit(speaker.name, _disfluent(" ".join(parts), reg, rng))
        if rng.chance(_backchannel_probability(reg.short_turn_fraction)):
            emit(rng.choice(people).name, rng.choice(grammar.BACKCHANNEL))

    emit(chair.name, _fill(rng.choice(templates["close"]), session, org, rng, names))
    meeting = _assemble(session, org, turns, reg, config)
    return meeting, placements


def _plant_positions(count: int, target_words: int, rng: Rng) -> list[int]:
    """Word-count thresholds at which plants fire, spread over the meeting body."""
    if count == 0:
        return []
    low, high = int(target_words * 0.08), int(target_words * 0.92)
    span = max(high - low, count)
    step = span / count
    return sorted(int(low + step * i + rng.random() * step * 0.6) for i in range(count))


def _emit_plant(
    entry: dict, emit, session: Session, org: Org, rng: Rng, names: list[str], reg: Register
) -> dict:
    """Emit the turns for one plant and return its placement record."""
    plant = entry["plant"]
    if entry["act"] == "fact":
        from .facts import ASPECTS_BY_NAME

        aspect = ASPECTS_BY_NAME[plant["aspect"]]
        pool = aspect.interactive if session.register == "interactive" else aspect.formal
        setup = _disfluent(rng.choice(SETUP_TEMPLATES).format(topic=plant["topic"]), reg, rng)
        setup_idx = emit(rng.choice(names), setup)
        body = _render_anchored(rng.choice(pool), plant["anchor"], reg, rng, topic=plant["topic"])
        answer_idx = emit(rng.choice(names), body)
        return {
            "kind": "fact",
            "fact_id": plant["fact_id"],
            "query_id": plant["query_id"],
            "file_uuid": session.file_uuid,
            "span": [setup_idx, answer_idx],
            "answer_turn": answer_idx,
            "anchor": plant["anchor"],
            "template": "aspect:" + aspect.name,
        }
    template = MARKER_TEMPLATES if entry["act"] == "marker" else EVENT_TEMPLATES
    chosen = rng.choice(template)
    idx = emit(
        rng.choice(names),
        _render_anchored(chosen.replace("{phrase}", "{anchor}"), plant["phrase"], reg, rng),
    )
    return {
        "kind": entry["act"],
        "query_id": plant["query_id"],
        "file_uuid": session.file_uuid,
        "span": [idx, idx],
        "answer_turn": idx,
        "anchor": plant["phrase"],
        "template": chosen,
    }


def _assemble(session: Session, org: Org, turns: list[dict], reg: Register, config: dict) -> dict:
    """Attach timestamps, speaker metadata and counts; return the meeting record."""
    rng = Rng(derive_seed(config["seed"], "timing", session.meeting_key))
    team = org.teams[session.team_id]
    people = {p.person_id: p for p in team.roster if p.person_id in session.attendees}
    by_name = {p.name: pid for pid, p in people.items()}
    clock = 0.0
    out_turns: list[dict] = []
    for i, turn in enumerate(turns):
        word_count = len(turn["content"].split())
        duration = max(0.6, word_count / reg.words_per_second) * (0.85 + 0.3 * rng.random())
        start = clock + (rng.random() * 1.4 - (0.35 if session.register == "interactive" else 0.0))
        start = max(start, 0.0 if i == 0 else out_turns[-1]["start"] + 0.25)
        end = start + duration
        out_turns.append(
            {
                "index": i,
                "speaker_id": by_name.get(turn["speaker"], "UNKNOWN"),
                "speaker": turn["speaker"],
                "start": round(start, 2),
                "end": round(end, 2),
                "content": turn["content"],
            }
        )
        clock = end
    words = sum(len(t["content"].split()) for t in out_turns)
    return {
        "file_uuid": session.file_uuid,
        "meeting_key": session.meeting_key,
        "corpus_id": config["corpus_id"],
        "title": f"{team.label.title()} — {session.series_kind} #{session.session_index + 1}",
        "series_id": session.series_id,
        "series_kind": session.series_kind,
        "team_id": session.team_id,
        "team_label": team.label,
        "division": team.division,
        "register": session.register,
        "session_index": session.session_index,
        "date": session.date,
        "start_second_of_day": session.start_second_of_day,
        "duration_seconds": round(out_turns[-1]["end"], 2) if out_turns else 0.0,
        "near_duplicate_cluster": session.cluster_id,
        "agenda": list(session.agenda),
        "speakers": [
            {"speaker_id": pid, "name": p.name, "role": p.role} for pid, p in sorted(people.items())
        ],
        "turn_count": len(out_turns),
        "word_count": words,
        "turns": out_turns,
    }
