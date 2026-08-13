"""The session-lifetime gate must be able to fail.

``scripts/audit-session-lifetime.py`` exists because holding a DB session across slow
non-DB work is this codebase's single most repeated defect — it has wedged the live
database twice in one day, on two different workers (48 min and 1 h 26 m ``idle in
transaction``), and it was found only because DDL migration tests started failing with
``psycopg2.errors.LockNotAvailable``.

A detector that silently stops matching reports **zero findings**, which reads exactly like
a clean codebase. That is the same class of failure the gate exists to catch, so this module
runs the auditor's own self-test under pytest: every detector gets a must-fire case, and the
real three-phase fix shapes must stay silent (a gate that punishes the cure gets switched
off). Four gates in this repo were already found silently not working; this is why the flag
is not enough on its own.

It also asserts the allowlist's ratchet properties directly, because "the list can only
shrink" is a claim about ``apply_allowlist``, not about the detectors.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "audit-session-lifetime.py"


def _load_auditor():
    spec = importlib.util.spec_from_file_location("audit_session_lifetime", _SCRIPT)
    assert spec and spec.loader, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_session_lifetime"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auditor():
    if not _SCRIPT.exists():
        pytest.fail(f"{_SCRIPT} is missing — the session-lifetime gate has no implementation")
    return _load_auditor()


def test_every_detector_is_alive(auditor):
    """The auditor's own self-test: must-fire and must-stay-clean, all cases."""
    failures = auditor.run_selftest(verbose=False)
    assert failures == [], "a session-lifetime detector is dead:\n  " + "\n  ".join(failures)


def test_every_category_has_a_must_fire_case(auditor):
    """A category with no self-test case can rot to silence unnoticed."""
    covered = {category for category, _ in auditor.SELFTEST_CASES}
    missing = sorted(set(auditor.CATEGORIES) - covered)
    assert not missing, f"categories with no must-fire self-test case: {missing}"


def test_every_rule_is_reachable_by_mutation(auditor):
    """Disable each rule in turn; its self-test case must then FAIL.

    A rule whose case still passes with the rule removed is being satisfied by some OTHER
    rule — it proves nothing about the detector it claims to cover.
    """
    original = auditor.RULES
    unreachable = []
    try:
        for index, rule in enumerate(original):
            disabled = replace(rule, names=frozenset(), paths=())
            auditor.RULES = original[:index] + (disabled,) + original[index + 1 :]
            failures = auditor.run_selftest(verbose=False)
            if not any(rule.category in line for line in failures):
                unreachable.append(rule.category)
    finally:
        auditor.RULES = original

    assert not unreachable, (
        "disabling these rules did not break their own self-test case, so the case does "
        f"not exercise them: {unreachable}"
    )


def test_interprocedural_rule_survives_rule_mutation(auditor):
    """The rule that caught ``scan_single`` is not covered by the loop above.

    ``session-param-slow-work`` has no ``Rule`` of its own — it reuses the slow-call rules
    from inside a function that accepts a session. Its own predicate is
    :func:`_takes_session`, so it needs its own mutation.
    """
    source = (
        "def _perform_scan(db, source, summary):\n"
        "    with create_client(source) as client:\n"
        '        client.download_file("a", "/tmp/b")\n'
    )
    assert any(f.category == "session-param-slow-work" for f in auditor.scan_source(source, "x.py"))

    original = auditor._takes_session
    try:
        auditor._takes_session = lambda _fn: None
        assert not any(
            f.category == "session-param-slow-work" for f in auditor.scan_source(source, "x.py")
        ), (
            "the interprocedural finding survived disabling _takes_session — it is coming from elsewhere"
        )
    finally:
        auditor._takes_session = original


def test_allowlist_is_count_aware(auditor):
    """One line buys ONE finding, so a partial fix cannot hide behind an old entry."""
    findings = [
        auditor.Finding("session-llm", "a.py", 10, "task", "generate_summary()"),
        auditor.Finding("session-llm", "a.py", 20, "task", "generate_summary()"),
    ]
    key = "a.py::task::session-llm"

    unallowed, backlog, accepted, stale, unreasoned = auditor.apply_allowlist(
        findings, {key: ["known"]}
    )
    assert len(unallowed) == 1, "one allowlist line covered two findings"
    assert accepted == 1
    assert stale == [] and unreasoned == [] and backlog == []

    unallowed, _backlog, accepted, stale, _unreasoned = auditor.apply_allowlist(
        findings, {key: ["known", "known"]}
    )
    assert unallowed == [] and accepted == 2 and stale == []


def test_allowlist_entry_with_no_finding_is_stale(auditor):
    """The ratchet: the file can only shrink. A surplus entry FAILS the run."""
    key = "a.py::task::session-llm"
    _unallowed, _backlog, _accepted, stale, _unreasoned = auditor.apply_allowlist(
        [], {key: ["known"]}
    )
    assert len(stale) == 1 and key in stale[0]

    # ...and a PARTIAL fix is visible too: two lines, one finding left.
    findings = [auditor.Finding("session-llm", "a.py", 10, "task", "generate_summary()")]
    _u, _b, _a, stale, _un = auditor.apply_allowlist(findings, {key: ["known", "known"]})
    assert len(stale) == 1 and "delete 1" in stale[0]


def test_allowlist_entry_without_a_reason_is_rejected(auditor):
    """An exemption nobody had to justify is one nobody will ever revisit."""
    findings = [auditor.Finding("session-llm", "a.py", 10, "task", "generate_summary()")]
    _u, _b, _a, _s, unreasoned = auditor.apply_allowlist(
        findings, {"a.py::task::session-llm": [""]}
    )
    assert unreasoned == ["a.py::task::session-llm"]


def test_backlog_reasons_are_reported_separately(auditor):
    """A deferred finding must never be counted as an accepted pattern."""
    findings = [auditor.Finding("session-llm", "a.py", 10, "task", "generate_summary()")]
    _u, backlog, accepted, _s, _un = auditor.apply_allowlist(
        findings, {"a.py::task::session-llm": ["BACKLOG not reached yet"]}
    )
    assert len(backlog) == 1 and accepted == 0


def test_the_six_fixed_tasks_are_clean(auditor):
    """The instances fixed alongside this gate must not reappear.

    Asserted against the real source, not a fixture: this is the regression guard for the
    fixes in ``test_task_session_lifetime.py``. ``watch_sources/processing.py`` is excluded
    on purpose — its ``ingest_prepared_file`` residual is a documented, allowlisted BACKLOG
    entry, and pretending otherwise here would be the false-clean signal this file exists
    to prevent.
    """
    app_root = Path(__file__).resolve().parents[2] / "app"
    fixed = [
        "tasks/summarization.py",
        "tasks/speaker_identification_task.py",
        "tasks/watch_source_tasks.py",
        "tasks/media_download.py",
        "tasks/cleanup.py",
        "tasks/transcription/embeddings.py",
        "services/video_processing_service.py",
    ]
    offenders: list[str] = []
    for rel in fixed:
        path = app_root / rel
        assert path.exists(), f"{rel} moved — this guard is now scanning nothing"
        for finding in auditor.scan_source(path.read_text(), rel):
            # clear_cache_for_media_file is separately allowlisted; it is not one of the six.
            if finding.key in auditor.load_allowlist():
                continue
            offenders.append(f"{finding.path}:{finding.line} {finding.scope} — {finding.detail}")
    assert not offenders, "a fixed session-lifetime leak came back:\n  " + "\n  ".join(offenders)
