"""The integration session fails when it leaves a different number of organizations behind.

``tests/integration/organization_count_guard.py`` exists because ``test_deletion_residue_live``
committed an organization per seeded file, which made the shared database multi-tenant and
broke single-tenant suites elsewhere. These pin both directions with the real hook bodies and a
stand-in for the database count: a moved count fails the session, an unchanged one does not,
and an xdist worker never decides (only the controller sees the whole run).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.integration import organization_count_guard as guard


class _Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str, **_markup: Any) -> None:
        self.lines.append(line)


def _session(*, worker: bool = False) -> Any:
    reporter = _Reporter()
    config = SimpleNamespace(
        stash=pytest.Stash(),
        pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter),
    )
    if worker:
        config.workerinput = {"workerid": "gw0"}
    return SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK, reporter=reporter)


def _run(monkeypatch: pytest.MonkeyPatch, counts: list[int | None], *, worker: bool = False):
    remaining = list(counts)
    monkeypatch.setattr(guard, "organization_count", lambda: remaining.pop(0))
    session = _session(worker=worker)
    guard.record_before(session)
    return session, guard.check_after(session)


@pytest.mark.unit
def test_a_leaked_organization_fails_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session, message = _run(monkeypatch, [0, 2])
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert message is not None and "from 0 to 2" in message
    assert session.reporter.lines == [f"ORGANIZATION COUNT GUARD: {message}"]


@pytest.mark.unit
def test_an_unchanged_count_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    session, message = _run(monkeypatch, [3, 3])
    assert (session.exitstatus, message, session.reporter.lines) == (pytest.ExitCode.OK, None, [])


@pytest.mark.unit
def test_an_unreachable_database_decides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    session, message = _run(monkeypatch, [None, 5])
    assert (session.exitstatus, message) == (pytest.ExitCode.OK, None)


@pytest.mark.unit
def test_an_xdist_worker_never_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    session, message = _run(monkeypatch, [], worker=True)
    assert (session.exitstatus, message) == (pytest.ExitCode.OK, None)
