"""Assert-inert: prove a deployment cannot start background work from a visit (issue #898).

The public demo's acceptance test (#628, exit criterion E1) is "a demo session cannot enqueue
any Celery task". This module is the machine-checkable form of that, in three parts:

1. **A publish counter.** ``record_task_published`` runs inside the one
   ``before_task_publish`` receiver in ``app/core/celery.py`` — the signal Celery sends
   "before the task is published" from every ``delay``/``apply_async``/``send_task`` (Celery
   docs, "Signals" -> ``before_task_publish``) — and increments
   ``celery_tasks_published_total{task}`` (``app/core/metrics.py``). :class:`PublishWindow`
   diffs that counter around a crawl. Before trusting a zero it sends a self-test publish
   through the same signal; when the receiver or the count is missing the result is
   **NOT MEASURED**, never a silent zero.
2. **A route reachability model** over the real app routes (``model_routes`` in
   ``app/api/inert_routes.py``, the HTTP layer, because it knows Starlette types). It
   branches on the Starlette route type explicitly: an HTTP ``Route`` has methods, a
   ``WebSocketRoute`` has none and is NOT seen by ``BaseHTTPMiddleware`` (Starlette's
   ``BaseHTTPMiddleware.__call__`` passes any non-``http`` scope straight through), so a
   demo guard written as HTTP middleware cannot cover it. A route kind it does not know
   fails closed. :mod:`app.core.inert_reach` finds, per route, a call chain to a publish.
3. **The beat plane.** Every ``BEAT_SCHEDULE`` entry carries a :class:`BeatEffect` in
   ``BEAT_ENTRY_EFFECT`` (``app/core/celery_beat.py``); an unclassified entry is a finding.

Run as a gate: ``python -m app.scripts.assert_inert [--json]`` in the backend container. Exit codes:
``0`` inert, ``1`` not inert, ``2`` usage error, ``3`` NOT MEASURED. The unit gate is
``tests/unit/test_inert_checker.py``. This module holds the framework-free parts and the
report; ``app/api/inert_routes.check`` composes them.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import Any

#: The self-test publish's sender. Never a real task name.
PROBE_TASK = "opentranscribe.inert.probe"

#: HTTP methods a visitor's browser issues without submitting anything. A demo that refuses
#: every other method is inert only if none of these reaches a publish.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The pseudo-method a WebSocket route is reported under.
WEBSOCKET = "WEBSOCKET"

EXIT_INERT = 0
EXIT_NOT_INERT = 1
EXIT_USAGE = 2
EXIT_NOT_MEASURED = 3


class Status(StrEnum):
    """The checker's verdict."""

    INERT = "INERT"
    NOT_INERT = "NOT INERT"
    NOT_MEASURED = "NOT MEASURED"


# --- 1. the publish counter ---------------------------------------------------------------


def record_task_published(task_name: Any) -> None:
    """Count one publish. Called from the ``before_task_publish`` receiver only."""
    from app.core import metrics

    metrics.celery_tasks_published_total.labels(task=str(task_name or "unknown")).inc()


def published_counts() -> dict[str, float]:
    """Publishes this process has made so far, by task name (the probe included)."""
    from app.core import metrics

    counts: dict[str, float] = {}
    for family in metrics.celery_tasks_published_total.collect():
        for sample in family.samples:
            if sample.name.endswith("_total"):
                counts[sample.labels["task"]] = sample.value
    return counts


def probe_publish_hook() -> bool:
    """True when a publish in this process is counted, proven by sending one through the signal.

    The probe goes through ``before_task_publish.send`` — the same call Celery makes on a real
    publish — and checks the counter moved, so a receiver that is connected but no longer
    counts reads as unmeasured too. Celery's ``Signal.send`` swallows receiver exceptions, so
    "did the count move" is the only trustworthy observation.
    """
    from celery.signals import before_task_publish

    before = published_counts().get(PROBE_TASK, 0.0)
    before_task_publish.send(sender=PROBE_TASK, headers={}, body=None)
    return published_counts().get(PROBE_TASK, 0.0) > before


@dataclass
class PublishMeasurement:
    """What a :class:`PublishWindow` saw."""

    measured: bool = False
    published: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        """Real publishes in the window (the probe excluded)."""
        return sum(self.published.values())

    @property
    def status(self) -> Status:
        """NOT MEASURED, NOT INERT (anything published) or INERT."""
        if not self.measured:
            return Status.NOT_MEASURED
        return Status.NOT_INERT if self.total else Status.INERT


class PublishWindow:
    """Count publishes made by this process between ``__enter__`` and ``__exit__``.

    ``with PublishWindow() as window: crawl(...)`` then read ``window.result``. The hook is
    probed at both ends, so it cannot be disconnected mid-crawl unnoticed.
    """

    def __init__(self) -> None:
        self.result = PublishMeasurement()
        self._start: dict[str, float] = {}
        self._probe_ok = False

    def __enter__(self) -> PublishWindow:
        self._probe_ok = probe_publish_hook()
        self._start = published_counts()
        return self

    def __exit__(self, *exc: object) -> None:
        end = published_counts()
        measured = self._probe_ok and probe_publish_hook()
        delta = {
            task: value - self._start.get(task, 0.0)
            for task, value in end.items()
            if task != PROBE_TASK and value > self._start.get(task, 0.0)
        }
        self.result = PublishMeasurement(measured=measured, published=delta)


# --- 2. the route reachability model (built by app/api/inert_routes.py) -----------------


@dataclass(frozen=True)
class RouteFinding:
    """One route as the model sees it."""

    kind: str
    methods: tuple[str, ...]
    path: str
    endpoint: str
    bypasses_http_middleware: bool
    witness: str | None

    @property
    def safe(self) -> bool:
        """Reachable by a visit: a safe HTTP method or a WebSocket."""
        return bool(set(self.methods) & (SAFE_METHODS | {WEBSOCKET}))

    @property
    def keys(self) -> list[str]:
        """``"METHOD path"`` for each safe method (the fixture's key shape)."""
        return [f"{m} {self.path}" for m in self.methods if m in SAFE_METHODS | {WEBSOCKET}]


# --- 3. the beat plane -------------------------------------------------------------------


def unclassified_beat_entries(
    schedule: dict[str, Any] | None = None, effects: dict[str, Any] | None = None
) -> list[str]:
    """Beat entries with no :class:`~app.core.celery_beat.BeatEffect` (or a stale one)."""
    from app.core.celery_beat import BEAT_ENTRY_EFFECT
    from app.core.celery_beat import BEAT_SCHEDULE
    from app.core.celery_beat import BeatEffect

    schedule = BEAT_SCHEDULE if schedule is None else schedule
    effects = BEAT_ENTRY_EFFECT if effects is None else effects
    missing = [name for name in schedule if not isinstance(effects.get(name), BeatEffect)]
    stale = [f"{name} (classified, not scheduled)" for name in effects if name not in schedule]
    return sorted(missing) + sorted(stale)


# --- the report --------------------------------------------------------------------------


@dataclass
class InertReport:
    """Everything one run found, plus the verdict."""

    hook_measured: bool
    publishing_safe_routes: list[RouteFinding]
    publishing_middleware: list[tuple[str, str, str]]
    unclassified_beat: list[str]
    beat_effects: dict[str, str]
    routes_modelled: int
    unresolved_imports: list[str]

    @property
    def status(self) -> Status:
        """NOT MEASURED wins: without a counter no crawl result means anything."""
        if not self.hook_measured:
            return Status.NOT_MEASURED
        if self.publishing_safe_routes or self.publishing_middleware or self.unclassified_beat:
            return Status.NOT_INERT
        return Status.INERT

    @property
    def exit_code(self) -> int:
        """The CLI exit code for :attr:`status`."""
        return {
            Status.INERT: EXIT_INERT,
            Status.NOT_INERT: EXIT_NOT_INERT,
            Status.NOT_MEASURED: EXIT_NOT_MEASURED,
        }[self.status]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "status": str(self.status),
            "exit_code": self.exit_code,
            "hook_measured": self.hook_measured,
            "routes_modelled": self.routes_modelled,
            "publishing_safe_routes": [asdict(f) for f in self.publishing_safe_routes],
            "publishing_middleware": [list(m) for m in self.publishing_middleware],
            "unclassified_beat": self.unclassified_beat,
            "beat_effects": self.beat_effects,
            "unresolved_imports": self.unresolved_imports,
        }


def render(report: InertReport) -> str:
    """The human-readable report."""
    lines = [f"assert-inert: {report.status} ({report.routes_modelled} routes modelled)"]
    if not report.hook_measured:
        lines.append("  publish counter: ABSENT -- no crawl result can be trusted")
    for finding in report.publishing_safe_routes:
        lines.append(f"  {', '.join(finding.keys)} [{finding.kind}]")
        lines.append(f"      {finding.witness}")
    for name, scope, witness in report.publishing_middleware:
        lines.append(f"  middleware {name} ({scope}): {witness}")
    for name in report.unclassified_beat:
        lines.append(f"  beat entry without a BeatEffect: {name}")
    if report.unresolved_imports:
        lines.append(f"  model gaps (unimportable): {', '.join(report.unresolved_imports)}")
    return "\n".join(lines)
