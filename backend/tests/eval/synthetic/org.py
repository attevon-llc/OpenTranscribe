"""The org model: divisions, teams, recurring series, and the session calendar.

Two design decisions here carry the whole distractor strategy, and both exist because of
the measured AMI pathology (``.rag-403/eval-corpus-plan.md`` §4: 137 near-duplicate
Product meetings drive BM25 R@1 to 0.124 against Committee's 0.664, with 49.2 other
meetings scoring within 10% of gold):

1. **Signature vocabulary is disjoint by construction.** Each team draws its components,
   projects and metrics without replacement from shared pools, so no two teams describe
   the same artefact. Teams are still *plausible* neighbours — they use identical process
   language, identical templates and identical registers — so they are distinguishable
   without being trivially separable. That is the "plausible but distinguishable"
   requirement.
2. **Near-duplication is a dial, not an accident.** ``near_duplicate_rate`` is the
   fraction of sessions placed into clusters that share an agenda skeleton and attendee
   list with their siblings. At 0.0 the corpus has no AMI-style structure at all; at 0.9
   it reproduces it deliberately. Sweeping it turns §4's confound into a measured curve.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field

from . import grammar
from .facts import AnchorAllocator
from .rng import Rng
from .rng import derive_seed

#: Fixed UUIDv5 namespace. Changing it renames every file in every generated corpus.
CORPUS_NAMESPACE = uuid.UUID("6f1d5a2e-9c34-5b7a-8e21-0f4b2d7c9a10")


@dataclass(frozen=True)
class Person:
    """A roster member."""

    person_id: str
    name: str
    role: str


@dataclass(frozen=True)
class Team:
    """A team and its disjoint signature vocabulary."""

    team_id: str
    label: str
    division: str
    roster: tuple[Person, ...]
    components: tuple[str, ...]
    projects: tuple[str, ...]
    metrics: tuple[str, ...]

    @property
    def topics(self) -> tuple[str, ...]:
        """Topic phrases that recur across this team's meetings."""
        return tuple(
            [f"the {c}" for c in self.components] + [f"the {p} programme" for p in self.projects]
        )


@dataclass
class Session:
    """One meeting to be rendered."""

    meeting_key: str
    file_uuid: str
    series_id: str
    series_kind: str
    team_id: str
    register: str
    session_index: int
    date: str
    start_second_of_day: int
    attendees: tuple[str, ...]
    agenda: tuple[str, ...]
    cluster_id: str | None
    plants: list[dict] = field(default_factory=list)


@dataclass
class Series:
    """A recurring meeting series for one team."""

    series_id: str
    team_id: str
    kind: str
    register: str
    sessions: list[Session]


@dataclass
class Org:
    """The generated organisation."""

    teams: dict[str, Team]
    series: dict[str, Series]
    sessions: list[Session]
    allocator: AnchorAllocator
    #: Series already consumed by a one-per-series rule (R6), so two aggregation
    #: questions cannot ask the same thing with the same gold set.
    used_series: set[str] = field(default_factory=set)

    def session_by_uuid(self, file_uuid: str) -> Session:
        """Return the session with the given file uuid."""
        for session in self.sessions:
            if session.file_uuid == file_uuid:
                return session
        raise KeyError(file_uuid)


_MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _iso_date(day_offset: int, start_year: int = 2025) -> str:
    """Return the ISO date ``day_offset`` days after 1 January ``start_year``.

    A local calendar rather than :mod:`datetime` so the corpus text cannot change if a
    future Python alters formatting defaults. Leap years are ignored on purpose: the
    generated calendar only has to be internally consistent and monotone, and the
    temporal aggregation answers are computed from these same strings.
    """
    year, remaining = start_year, day_offset
    while True:
        year_len = 365
        if remaining < year_len:
            break
        remaining -= year_len
        year += 1
    month = 0
    while remaining >= _MONTH_DAYS[month]:
        remaining -= _MONTH_DAYS[month]
        month += 1
    return f"{year:04d}-{month + 1:02d}-{remaining + 1:02d}"


def _make_team(
    rng: Rng, index: int, division: str, pools: dict[str, list[str]], allocator: AnchorAllocator
) -> Team:
    """Build one team, consuming its signature vocabulary from the shared pools."""
    domain = pools["domains"].pop()
    roster_size = rng.randint(7, 14)
    roster = tuple(
        Person(f"T{index:03d}P{i:02d}", allocator.allocate("person"), rng.choice(grammar.ROLE))
        for i in range(roster_size)
    )
    components = tuple(pools["components"].pop() for _ in range(rng.randint(4, 6)))
    projects = tuple(allocator.allocate("codename") for _ in range(rng.randint(2, 3)))
    metrics = tuple(rng.sample(grammar.METRIC_NAME, 4))
    return Team(
        team_id=f"T{index:03d}",
        label=f"the {domain} team",
        division=division,
        roster=roster,
        components=components,
        projects=projects,
        metrics=metrics,
    )


def _component_pool(rng: Rng) -> list[str]:
    """All qualifier/head/tail component phrases, shuffled — popped without replacement."""
    return rng.shuffled(
        [
            f"{q} {h} {t}"
            for q in grammar.COMPONENT_QUAL
            for h in grammar.COMPONENT_HEAD
            for t in grammar.COMPONENT_TAIL
        ]
    )


def _domain_pool(rng: Rng, needed: int) -> list[str]:
    """Team domain labels, extended with a numeric suffix if more teams than labels."""
    base = rng.shuffled(grammar.TEAM_DOMAIN)
    out = list(base)
    round_index = 2
    while len(out) < needed:
        out.extend(f"{d}-{round_index}" for d in base)
        round_index += 1
    return out[:needed]


def build_org(config: dict) -> Org:
    """Plan the organisation and its full session calendar.

    Args:
        config: The validated corpus config (see ``corpus.default_config``).

    Returns:
        An :class:`Org` whose ``sessions`` list is exactly ``config["meetings"]`` long
        and sorted by ``meeting_key``.
    """
    rng = Rng(derive_seed(config["seed"], "org"))
    allocator = AnchorAllocator(Rng(derive_seed(config["seed"], "anchors")))
    n_meetings = config["meetings"]
    n_teams = max(4, round(n_meetings / config["meetings_per_team"]))
    pools = {"components": _component_pool(rng), "domains": _domain_pool(rng, n_teams)}

    teams: dict[str, Team] = {}
    for i in range(n_teams):
        division = grammar.DIVISION_NAME[i % len(grammar.DIVISION_NAME)]
        team = _make_team(rng, i, division, pools, allocator)
        teams[team.team_id] = team

    series: dict[str, Series] = {}
    sessions: list[Session] = []
    per_team = n_meetings // n_teams
    leftover = n_meetings - per_team * n_teams
    for i, team in enumerate(teams.values()):
        budget = per_team + (1 if i < leftover else 0)
        sessions.extend(_build_team_series(rng, team, budget, series, config))
    sessions.sort(key=lambda s: s.meeting_key)
    return Org(teams=teams, series=series, sessions=sessions, allocator=allocator)


def _build_team_series(
    rng: Rng, team: Team, budget: int, series: dict[str, Series], config: dict
) -> list[Session]:
    """Split a team's meeting budget across 2-5 recurring series."""
    kinds = rng.sample(
        grammar.SERIES_KIND, min(len(grammar.SERIES_KIND), max(2, rng.randint(2, 5)))
    )
    out: list[Session] = []
    for si, (kind, register) in enumerate(kinds):
        count = budget // len(kinds) + (1 if si < budget % len(kinds) else 0)
        if count == 0:
            continue
        series_id = f"{team.team_id}-S{si}"
        built = _build_sessions(rng, team, series_id, kind, register, count, config)
        series[series_id] = Series(series_id, team.team_id, kind, register, built)
        out.extend(built)
    return out


def _build_sessions(
    rng: Rng, team: Team, series_id: str, kind: str, register: str, count: int, config: dict
) -> list[Session]:
    """Lay out one series' sessions on the calendar, applying the near-duplicate dial."""
    cadence = 7 if register == "interactive" else 28
    day = rng.randint(0, 20)
    cluster_head: Session | None = None
    built: list[Session] = []
    for index in range(count):
        meeting_key = f"{series_id}-{index:04d}"
        file_uuid = str(uuid.uuid5(CORPUS_NAMESPACE, f"{config['corpus_id']}:{meeting_key}"))
        in_cluster = cluster_head is not None and rng.chance(config["near_duplicate_rate"])
        if in_cluster and cluster_head is not None:
            attendees, agenda = cluster_head.attendees, cluster_head.agenda
            cluster_id = cluster_head.meeting_key
        else:
            reg = grammar.REGISTERS[register]
            size = rng.randint(reg.speakers_low, min(reg.speakers_high, len(team.roster)))
            attendees = tuple(sorted(p.person_id for p in rng.sample(team.roster, size)))
            agenda = tuple(rng.sample(team.topics, min(len(team.topics), rng.randint(2, 4))))
            cluster_id = None
        session = Session(
            meeting_key=meeting_key,
            file_uuid=file_uuid,
            series_id=series_id,
            series_kind=kind,
            team_id=team.team_id,
            register=register,
            session_index=index,
            date=_iso_date(day),
            start_second_of_day=3600 * rng.randint(9, 16) + 900 * rng.randint(0, 3),
            attendees=attendees,
            agenda=agenda,
            cluster_id=cluster_id,
        )
        if cluster_id is None:
            cluster_head = session
        else:
            # Mark the head too, so "how many meetings are in a near-duplicate cluster"
            # counts the original rather than only its copies.
            cluster_head.cluster_id = cluster_id  # type: ignore[union-attr]
        built.append(session)
        day += cadence + rng.randint(-2, 2)
    _ensure_strict_attendance_max(built, team)
    return built


def _ensure_strict_attendance_max(sessions: list[Session], team: Team) -> None:
    """Give every series a single, strictly-most-frequent attendee.

    Rule R6 asks "who attended the most sessions of this series?", and a tied maximum has
    two correct answers — a question with an ambiguous gold answer is precisely what this
    tier exists to avoid. Rejecting tied series at query-planning time was not enough: on
    a 60-meeting corpus *every* eligible series tied, R6 emitted nothing, and check V5
    went unexercised while still reading as a clean pass.

    The fix is structural and realistic: the chair attends every session of their own
    series, and anyone else with perfect attendance is dropped from one session.
    """
    if not sessions:
        return
    chair = team.roster[0].person_id
    for session in sessions:
        if chair not in session.attendees:
            session.attendees = tuple(sorted({*session.attendees, chair}))
    total = len(sessions)
    counts: dict[str, int] = {}
    for session in sessions:
        for person_id in session.attendees:
            counts[person_id] = counts.get(person_id, 0) + 1
    for person_id in sorted(p for p, c in counts.items() if c == total and p != chair):
        for session in reversed(sessions):
            if len(session.attendees) > 3:
                session.attendees = tuple(a for a in session.attendees if a != person_id)
                break
