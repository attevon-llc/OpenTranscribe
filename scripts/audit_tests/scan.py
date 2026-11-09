"""Run every detector over a module (scan_source / scan_file)."""

from __future__ import annotations

import ast
from pathlib import Path

from audit_tests.common import assert_nodes, has_raises_or_helper, is_fixture, is_test_function
from audit_tests.findings import Finding
from audit_tests.misc import bare_returns, broad_raises, self_comparison, skip_markers, sleep_syncs
from audit_tests.patching import (
    MAX_PATCH_REFS,
    external_service_mock,
    marker_claims,
    patch_refs,
    pytestmark_values,
    services_by_fixture,
)
from audit_tests.probe import readiness_probe_targets
from audit_tests.status import (
    MAX_STATUS_ALTERNATIVES,
    conditional_only,
    conditional_skip,
    negated_status,
    status_alternatives,
    status_guarded_asserts,
)
from audit_tests.vacuous import (
    loop_only,
    masks_failure,
    mock_only,
    single_assignments,
    swallows_error,
    unfalsifiable,
    weak_only,
)


def scan_source(source: str, rel: str) -> list[Finding]:
    """Scan test source text. Helper functions are scanned for failure-masking too.

    Split out from ``scan_file`` so ``--selftest`` can drive every detector from in-memory
    fixtures. Fixtures on disk would be collected by pytest and linted as real tests.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[Finding] = []
    # Module-level tables (`EXPORT_FORMATS = [...]`) are as static as an inline literal.
    module_consts = single_assignments(
        [s for s in tree.body if isinstance(s, ast.Assign | ast.AnnAssign)]
    )
    # `pytest.mark.integration` on the module or the class reaches every test inside it, so a
    # decorator-only read of the claim would miss whole suites.
    module_claims = marker_claims(pytestmark_values(tree))
    # Which fixture in this module substitutes which service — see `services_by_fixture`.
    fixture_services = services_by_fixture(tree)
    class_claims: dict[int, set[str]] = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        claims = marker_claims([*cls.decorator_list, *pytestmark_values(cls)])
        if not claims:
            continue
        for inner in ast.walk(cls):
            if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef):
                class_claims.setdefault(id(inner), set()).update(claims)

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        is_test = is_test_function(node)
        if node.name.startswith('test_') and is_fixture(node):
            found.append(
                Finding(
                    'fixture-named-test',
                    rel,
                    node.lineno,
                    node.name,
                    'fixture named test_* — reads as a test, never runs as one',
                )
            )

        # Failure masking matters in helpers too — that is where the import guards live.
        caught = masks_failure(node)
        if caught:
            found.append(
                Finding(
                    'failure-masking',
                    rel,
                    node.lineno,
                    node.name,
                    f'except {caught} -> pytest.skip',
                )
            )

        # Probes live in helpers and fixtures more often than in tests, so this one is not
        # gated on `is_test`.
        for lineno, target in readiness_probe_targets(node, node.name, module_consts):
            found.append(
                Finding(
                    'readiness-probe-target',
                    rel,
                    lineno,
                    node.name,
                    f'{target} — hardcoded endpoint, not the stack under test',
                )
            )

        # A sleep in a `_wait_for(predicate)` helper or a session fixture is the same bet as
        # one in a test body, so this is not gated on `is_test` either.
        for lineno, call in sleep_syncs(node):
            found.append(
                Finding(
                    'sleep-sync',
                    rel,
                    lineno,
                    node.name,
                    f'{call} — a fixed wait, not a poll loop; nothing re-checks readiness',
                )
            )
        if not is_test:
            continue

        for lineno, marker in skip_markers(node):
            found.append(Finding('skipped-test', rel, lineno, node.name, marker))

        for lineno, caught in broad_raises(node):
            found.append(
                Finding(
                    'broad-raises',
                    rel,
                    lineno,
                    node.name,
                    f'pytest.raises({caught}) — the setup inside the block satisfies it too',
                )
            )

        for lineno in bare_returns(node):
            found.append(
                Finding(
                    'bare-return',
                    rel,
                    lineno,
                    node.name,
                    'bare `return` before any assert or skip — exits green having proved nothing',
                )
            )

        identical = self_comparison(node)
        if identical is not None:
            lineno, expression = identical
            found.append(
                Finding(
                    'self-comparison',
                    rel,
                    lineno,
                    node.name,
                    f'sole assertion is `{expression} == {expression}` — true by construction',
                )
            )

        for lineno, count in status_alternatives(node):
            if count > MAX_STATUS_ALTERNATIVES:
                found.append(
                    Finding(
                        'permissive-status', rel, lineno, node.name, f'accepts {count} statuses'
                    )
                )

        for lineno, detail in negated_status(node):
            found.append(
                Finding(
                    'negated-status',
                    rel,
                    lineno,
                    node.name,
                    f'status_code {detail} — a 500 passes this',
                )
            )

        for lineno, guard, n_asserts in status_guarded_asserts(node):
            found.append(
                Finding(
                    'status-guarded-assert',
                    rel,
                    lineno,
                    node.name,
                    f'{n_asserts} assert(s) only run when `{guard}`',
                )
            )

        if conditional_only(node):
            found.append(
                Finding(
                    'conditional-only',
                    rel,
                    node.lineno,
                    node.name,
                    f'{len(assert_nodes(node))} assert(s), all inside if-without-else',
                )
            )

        skip_guard = conditional_skip(node)
        if skip_guard is not None:
            found.append(
                Finding(
                    'conditional-skip',
                    rel,
                    node.lineno,
                    node.name,
                    f'all asserts under `if {skip_guard}` whose else only skips',
                )
            )

        iterable = loop_only(node, module_consts)
        if iterable is not None:
            found.append(
                Finding(
                    'loop-only',
                    rel,
                    node.lineno,
                    node.name,
                    f'all asserts inside `for ... in {iterable}` — empty iterable passes',
                )
            )

        if not assert_nodes(node) and not has_raises_or_helper(node):
            found.append(
                Finding('no-assertion', rel, node.lineno, node.name, 'no assert/raises/assert_*')
            )

        for lineno, detail in unfalsifiable(node):
            found.append(
                Finding(
                    'unfalsifiable', rel, lineno, node.name, f'`{detail}` — every operand constant'
                )
            )

        weak = weak_only(node)
        if weak is not None:
            found.append(
                Finding(
                    'weak-only', rel, node.lineno, node.name, f'only bare-truthy asserts: {weak}'
                )
            )

        n_bookkeeping = mock_only(node)
        if n_bookkeeping is not None:
            found.append(
                Finding(
                    'mock-only',
                    rel,
                    node.lineno,
                    node.name,
                    f'{n_bookkeeping} assertion(s), all mock bookkeeping',
                )
            )

        swallowed = swallows_error(node)
        if swallowed:
            found.append(
                Finding(
                    'error-swallowed',
                    rel,
                    node.lineno,
                    node.name,
                    f'except {swallowed} -> pass/return/log only',
                )
            )

        refs = patch_refs(node)
        if refs >= MAX_PATCH_REFS:
            found.append(Finding('mock-heavy', rel, node.lineno, node.name, f'{refs} patch calls'))

        mislabelled = external_service_mock(
            node, rel, module_claims | class_claims.get(id(node), set()), fixture_services
        )
        if mislabelled is not None:
            services, evidence = mislabelled
            found.append(
                Finding(
                    'external-service-mock',
                    rel,
                    node.lineno,
                    node.name,
                    f'id claims real {services}, but {evidence}',
                )
            )

    # A conftest's service detection runs at import time, outside any function — and that is
    # where the probe that decides which stack the whole suite talks to actually lives. The
    # whole tree is passed: `readiness_probe_targets` drops calls owned by an inner function,
    # which is what keeps a probe inside a test METHOD from being reported here as well.
    for lineno, target in readiness_probe_targets(tree, '<module>', module_consts):
        found.append(
            Finding(
                'readiness-probe-target',
                rel,
                lineno,
                '<module>',
                f'{target} — hardcoded endpoint, not the stack under test',
            )
        )

    return found


def scan_file(path: Path, root: Path) -> list[Finding]:
    """Scan one test module."""
    try:
        source = path.read_text()
    except UnicodeDecodeError:
        return []
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    return scan_source(source, rel)
