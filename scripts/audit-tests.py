#!/usr/bin/env python3
"""Find tests that pass whether the code works or not.

A test that cannot fail is worse than no test: it buys false confidence and hides the
defect it was written to catch. This scans the test tree by AST for the patterns below.

Detectors
    permissive-status
        ``assert response.status_code in (200, 403)`` accepts success AND authorization
        failure. Concentrated in the security suites, where it matters most.
    negated-status
        ``assert response.status_code != 403``. Reads like an authorization assertion and
        is not one: a 401, a 404 and — the reason this matters — a **500** all pass. Fifteen
        of these were in this tree and none was visible to ``permissive-status``, which only
        looks at ``in (...)``.
    status-guarded-assert
        The real assertion sits inside ``if response.status_code == 200:`` with no ``else``.
        ``conditional-only`` cannot see it, because the test ALSO has an unguarded (and
        usually negated) status assert — so the shape survived the fix applied to its
        siblings. A wrong status silently skips the only check that matters.
    conditional-only
        Every assertion in the test sits inside an ``if`` with no ``else``, so the test
        passes silently whenever the condition is False.
    conditional-skip
        Every assertion sits inside an ``if`` whose ``else`` only calls ``pytest.skip``.
        Not vacuous like conditional-only — it *reports* a skip — but a guard that can
        never be true makes it a permanent skip that reads as a passing suite.
    loop-only
        Every assertion sits inside a ``for`` over a **dynamic** iterable. An empty
        iterable executes the body zero times, so the test is a vacuous pass:
        ``for span in detector.detect_pii(...): assert 0 <= span.start`` is green when
        nothing at all was detected. A loop over a literal collection is exempt — its
        length is fixed at parse time.
    no-assertion
        No ``assert``, no ``pytest.raises``/``pytest.fail``, no ``expect()``, no
        ``assert_*`` helper.
    unfalsifiable
        Both sides of the assertion are compile-time constants — ``assert True``,
        ``assert 1 == 1``. Cannot fail, ever.
    weak-only
        The test's ONLY assertions are bare-truthy (``assert still_on_login``) or
        ``is not None``. ``still_on_login = "/login" in page.url or
        page.locator("#email").is_visible()`` is an ``or`` chain over two things that are
        both true on a page that never navigated — including one that is true on a *crashed*
        page — so the assertion is a formality. Anything stronger in the test clears it.
    mock-only
        Every assertion is mock bookkeeping (``assert_called_once_with``, ``.called``,
        ``.call_args``) with nothing asserted about a return value or real state. Proves the
        test called the mock, not that the code works.
    failure-masking
        ``except ...: pytest.skip(...)`` reports a *failure* as a *skip*. A rename or a
        genuine regression then reads as "skipped" forever. Helper functions are scanned
        too — that is where import guards hide.
    error-swallowed
        ``except ...: pass`` / bare ``return`` / log-and-continue inside a **test**. The
        untested twin of failure-masking: it does not even report a skip, so the assertions
        after the ``try`` simply never run and the test is green.
    mock-heavy
        So many ``patch``/``monkeypatch`` calls in one test that the test asserts its own
        mock wiring rather than behaviour.
    fixture-named-test
        A ``@pytest.fixture`` named ``test_*``. It never runs as a test but reads as one,
        and it corrupts any count of tests-without-assertions.
    external-service-mock
        A test whose **id claims integration with an external service while that service is
        substituted**. The convention (issue #431, decided from the #403 RAG work): "ran
        against the real engine" and "ran against a stand-in" must be **different test ids** —
        a stand-in is never quietly substituted under the same id. Twelve tests for an
        OpenSearch ``delete_by_query`` body once ran entirely against an in-memory stand-in
        under ids that read as real-engine coverage; the suite was green on an assumption about
        the engine, and neither JUnit history nor the timing analyser could see it. A claim is
        only counted where the test DECLARES one — a marker/env gate, a realness word in the
        test's own name, or the module path. The service's own name is deliberately NOT a
        claim: it names the subject, not the engine.
    readiness-probe-target
        A health/readiness probe whose **target is hardcoded instead of derived from the stack
        under test**. ``wait_for_bench_backend_health`` polled ``opentranscribe-backend`` — the
        DEV stack — so the bench stack's readiness wait returned "healthy" whenever dev was up,
        regardless of whether the stack under test had started at all. That green-lights a
        benchmark against a stack that may not exist. Fires only when EVERY argument of the
        probe call is fixed at parse time; one derived argument means the target follows the
        stack under test.
    broad-raises
        ``pytest.raises(Exception)`` / ``(BaseException)``. The block asserts only that
        *something* went wrong, and **the setup inside the block counts**: a renamed keyword
        argument, a missing fixture attribute or a DB error all read as the refusal the test
        claims to prove. ruff's B017 says the same thing, and one of these carries a bare
        a bare ruff ``B017`` suppression with no reason, which is how the shape survived.
        ``match=`` clears it: pinning the message is a real constraint on *which* failure.
    bare-return
        A bare ``return`` in a test body reached before the test has asserted or skipped
        anything. ``conditional-skip`` only sees ``pytest.skip``, so this is the silent twin:
        the test exits green having proved nothing and reporting nothing. A ``return`` that
        follows an assertion (the second half of the test is conditional) or a ``pytest.skip``
        (``return  # unreachable, satisfies mypy``) is NOT this — both were checked against the
        real tree, and firing on them would have made the detector useless.
    self-comparison
        The test's SOLE assertion compares an expression to a syntactically identical one:
        ``assert _key_with_corpus("7") == _key_with_corpus("7")`` in a test named
        "cache key is stable within one corpus version" stays green if the key ignores the
        corpus version entirely. Only when it is the only assertion — the same comparison
        beside two real ones is a deliberate determinism check, not a vacuous test.
    sleep-sync
        ``time.sleep``/``asyncio.sleep`` used as **synchronisation** rather than as a poll
        interval. A bare sleep before an assertion bounds *correctness*: if the thing is not
        ready in 0.5 s the assertion runs anyway and the test fails (or, worse, passes for the
        wrong reason on a fast machine). A sleep inside a bounded poll loop that re-checks and
        exits early bounds only *latency*, so it is exempt — as is the whole Playwright
        ``wait_for_timeout`` idiom, which precedes auto-retrying ``expect()`` assertions.
    skipped-test
        An unconditional ``@pytest.mark.skip``, or an ``@pytest.mark.xfail`` without
        ``strict=True``. Both report a colour that is not "failed" forever. A non-strict xfail
        is the subtler one: it also passes when the test starts PASSING, so the day the bug is
        fixed nothing tells you the marker can go. The frontend auditor has had this detector
        since #431 and the backend did not — an asymmetry, not a decision.

Usage::

    scripts/audit-tests.py backend/tests
    scripts/audit-tests.py backend/tests --json
    scripts/audit-tests.py backend/tests --category permissive-status
    scripts/audit-tests.py --selftest        # audit the auditor (no tree needed)

``tests/e2e`` is scanned by DEFAULT (``--no-e2e`` opts out). It used to be excluded, which
hid 21 findings in the suite that drives a real browser — including the conditional-only
assertions around the media-download hang. A detector that does not look at the riskiest
tests in the tree is not a gate.

Exits 1 when any finding is not in the allowlist, so this can gate a commit. The allowlist
lives at ``backend/tests/audit-allowlist.txt``: one ``<file>::<test>::<category>  # reason``
per line. The category is REQUIRED — an entry keyed only by test would exempt that test from
every detector at once, which is how a `failure-masking` exemption silently granted a
`no-assertion` one too. Adding an entry is a deliberate, reviewable act; widening an
assertion to restore green is not.

**The allowlist is COUNT-AWARE: one line buys one finding.** A key is not a blanket. The set
difference this used to do could not see a PARTIAL fix — a key producing three findings and a
key producing one were the same key — so fixing two of
``test_personal_file_read_surfaces_unaffected``'s three ``!=`` assertions left the run green
with the exemption still covering the third. Duplicate keys are therefore MEANINGFUL, not an
error: three identical lines mean "three known findings here". Fix one and the surplus line is
reported as stale, exactly like an entry whose finding vanished entirely. That is what makes
"this file can only shrink" true per-finding rather than per-test-name.

**Run ``--selftest`` after touching any detector.** Its frontend sibling's self-test caught
two detectors matching *nothing*, which reports 0 findings and is indistinguishable from a
clean suite — the exact failure mode this script exists to prevent. Every detector needs a
case that must fire and the tree of clean cases must stay silent.
``backend/tests/unit/test_audit_tests_selftest.py`` runs the same cases under pytest, so the
suite fails if a detector goes blind even when nobody remembers the flag.
"""

from __future__ import annotations

import sys

from audit_tests.cli import main

if __name__ == '__main__':
    sys.exit(main(__doc__ or ''))
