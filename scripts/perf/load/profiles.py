"""Named load profiles (locust-free): how many simulated people, how fast, for how long.

``locustfile.ProfileShape`` (a Locust ``LoadTestShape``) asks the profile chosen with
``--load-profile NAME`` for the user count, spawn rate and user classes on every tick (Locust docs,
"Custom load shapes"). The profiles live here, without Locust, so the backend's unit tests can
step through them.

A profile is a list of stages. Each stage holds a user count from the moment it starts until
``until`` seconds into the run; after the last stage the run stops.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The personas of an ordinary web-tier run (the pipeline uploader is excluded: it sends real
#: recordings through the GPU workers and has a profile of its own).
WEB_PERSONAS = (
    'Viewer',
    'HeavyOwner',
    'Editor',
    'ChatUser',
    'Administrator',
    'OrgAdmin',
    'WebSocketListener',
)
PIPELINE_PERSONAS = ('PipelineUploader',)


@dataclass(frozen=True)
class Stage:
    until: float  # seconds into the run at which this stage ends
    users: int
    rate: float  # users started (or stopped) per second on the way to ``users``


@dataclass(frozen=True)
class Params:
    """The ``--target-users``, ``--ramp-rate``, ``--hold`` and ``--steps`` options (None = default)."""

    users: int | None = None
    rate: float | None = None
    hold: float | None = None
    steps: tuple[int, ...] | None = None

    @classmethod
    def from_options(cls, options: Any) -> Params:
        steps = getattr(options, 'steps', None)
        return cls(
            users=getattr(options, 'target_users', None),
            rate=getattr(options, 'ramp_rate', None),
            hold=getattr(options, 'hold', None),
            steps=tuple(int(s) for s in steps.split(',')) if steps else None,
        )


@dataclass(frozen=True)
class Profile:
    name: str
    summary: str
    personas: tuple[str, ...]
    chat_users: int  # default fixed count of ChatUser for this profile
    build: Callable[[Params], list[Stage]]

    def stages(self, params: Params) -> list[Stage]:
        stages = self.build(params)
        if not stages or any(b.until <= a.until for a, b in zip(stages, stages[1:], strict=False)):
            raise ValueError(f'profile {self.name!r} built stages that do not move forward')
        return stages

    def tick(self, run_time: float, params: Params) -> tuple[int, float, tuple[str, ...]] | None:
        """``(users, spawn rate, persona class names)`` at ``run_time``, or None to stop."""
        for stage in self.stages(params):
            if run_time < stage.until:
                return stage.users, stage.rate, self.personas
        return None


def _ramp_time(users: int, rate: float) -> float:
    return users / rate if rate > 0 else 0.0


def _smoke(p: Params) -> list[Stage]:
    users, rate = p.users or 10, p.rate or 2.0
    return [Stage(until=60.0, users=users, rate=rate)]


def _steady(p: Params) -> list[Stage]:
    users, rate, hold = p.users or 100, p.rate or 2.0, p.hold or 900.0
    return [Stage(until=_ramp_time(users, rate) + hold, users=users, rate=rate)]


def _ramp(p: Params) -> list[Stage]:
    steps, rate, hold = p.steps or (100, 250, 500, 1000), p.rate or 2.0, p.hold or 600.0
    stages, clock, previous = [], 0.0, 0
    for users in steps:
        clock += _ramp_time(abs(users - previous), rate) + hold
        stages.append(Stage(until=clock, users=users, rate=rate))
        previous = users
    return stages


def _spike(p: Params) -> list[Stage]:
    users, hold = p.users or 500, p.hold or 120.0
    rate = p.rate or users / 10.0  # the whole crowd arrives within about ten seconds
    up = _ramp_time(users, rate) + hold
    # Back to nobody, then a quiet tail so the sampler records how the stack recovers.
    return [
        Stage(until=up, users=users, rate=rate),
        Stage(until=up + _ramp_time(users, rate) + 120.0, users=0, rate=rate),
    ]


def _soak(p: Params) -> list[Stage]:
    users, rate, hold = p.users or 200, p.rate or 1.0, p.hold or 5400.0
    return [Stage(until=_ramp_time(users, rate) + hold, users=users, rate=rate)]


def _pipeline(p: Params) -> list[Stage]:
    users, rate, hold = p.users or 4, p.rate or 1.0, p.hold or 1800.0
    return [Stage(until=_ramp_time(users, rate) + hold, users=users, rate=rate)]


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile('smoke', 'a few people for one minute', WEB_PERSONAS, 1, _smoke),
        Profile('steady', '--target-users held for --hold seconds', WEB_PERSONAS, 8, _steady),
        Profile('ramp', 'step through --steps, holding each', WEB_PERSONAS, 8, _ramp),
        Profile('spike', '0 to --target-users at once, then back to 0', WEB_PERSONAS, 8, _spike),
        Profile('soak', 'moderate load for 90 minutes', WEB_PERSONAS, 8, _soak),
        Profile(
            'pipeline',
            'concurrent real uploads through the GPU pipeline',
            PIPELINE_PERSONAS,
            0,
            _pipeline,
        ),
    )
}


def get(name: str) -> Profile:
    """The profile called ``name``; ``KeyError`` lists the valid names."""
    if name not in PROFILES:
        raise KeyError(f'unknown profile {name!r}; choose one of {", ".join(PROFILES)}')
    return PROFILES[name]
