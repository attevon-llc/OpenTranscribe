#!/usr/bin/env node
/**
 * Find frontend tests that pass whether the code works or not.
 *
 * The vitest/TypeScript counterpart of `scripts/audit-tests.py` (which does this for
 * pytest). A test that cannot fail is worse than no test: it buys false confidence and
 * hides the defect it was written to catch. This parses every `*.{test,spec}.{ts,js}`
 * under `src/` with the TypeScript compiler API — no regex heuristics, and no new
 * dependency: `typescript` is already a devDependency.
 *
 * Detectors
 *   only-leak
 *       `it.only` / `describe.only`. Vitest then runs ONLY that test and reports the rest
 *       of the file as passing-by-omission. A single committed `.only` silently disables
 *       everything around it, so this is the highest-severity finding here.
 *   skipped-test
 *       `it.skip` / `describe.skip` / `it.todo`. Every one needs a written reason.
 *   unfalsifiable
 *       Both sides of the assertion are literals — `expect(true).toBe(true)`,
 *       `expect(1).toBe(1)`, `expect('x').toBeTruthy()`. Cannot fail, ever.
 *   weak-only
 *       The test's ONLY assertion is a matcher that passes on almost any value:
 *       `toBeDefined`, `toBeTruthy`/`toBeFalsy`, `not.toBeNull`, `not.toBeUndefined`,
 *       `not.toThrow`. `chatStream.test.ts` asserted `expect(headers['X-CSRF-Token'])
 *       .toBeDefined()` in a test named "sends the CSRF header" — and `''` is defined, so
 *       it passed while sending no token at all (issue #431).
 *   conditional-only
 *       Every assertion sits inside an `if` with no `else`, so the test passes silently
 *       whenever the condition is false.
 *   conditional-skip
 *       Every assertion sits inside an `if` whose `else` only skips (`ctx.skip()`) or
 *       returns, or behind an early-return guard. Not vacuous like conditional-only — it
 *       *reports* — but a guard that can never be true is a permanent skip that reads as a
 *       passing suite.
 *   floating-async-assertion
 *       `expect(p).rejects.toThrow()` without `await`. The matcher returns a promise
 *       nobody waits on, so the assertion resolves after the test has already passed.
 *       Unlike the others this is invisible in review: the assertion is right there.
 *   mock-heavy
 *       So many `vi.fn`/`vi.mock`/`vi.spyOn`/`mock*` references in one test that the test
 *       asserts its own mock wiring rather than behaviour.
 *   mock-only
 *       Every assertion's subject is a mock and every matcher is a call assertion
 *       (`toHaveBeenCalled*`), with nothing asserted about rendered output or a return
 *       value. Proves the test called the mock, not that the component works.
 *
 * Usage:
 *   node scripts/audit-frontend-tests.mjs
 *   node scripts/audit-frontend-tests.mjs src/components --list
 *   node scripts/audit-frontend-tests.mjs --json
 *   node scripts/audit-frontend-tests.mjs --category weak-only
 *
 * Exits 1 when any finding is not in the allowlist, so this can gate a commit. The
 * allowlist lives at `frontend/test-audit-allowlist.txt`: one
 * `<file>::<full test name>::<category>  # reason` per line.
 *
 * THE ALLOWLIST CAN ONLY SHRINK, AND ONE LINE BUYS ONE FINDING. Both rails are ported from
 * the Python side, and this file had NEITHER — `rg stale` in here returned nothing — while
 * the allowlist presented itself as the counterpart of a file whose central guarantee is that
 * an exemption cannot outlive its subject. Without them an entry survived its finding
 * indefinitely, and a `<file>::<test>::<category>` key silently pre-exempted any FUTURE test
 * that happened to reuse the name — a describe/it title is a string anyone can retype.
 *
 *   - An entry with no finding left to cover FAILS the run. Delete the line.
 *   - Duplicate keys are a COUNT, not a mistake: three lines mean three known findings. Fix
 *     one and the surplus line is reported, which is the partial fix a set difference cannot
 *     see (a key producing three findings and one producing one are the same key to a set).
 *   - A reason starting `BACKLOG` marks deferred work and is counted and printed separately,
 *     so a green gate is never read as a clean tree.
 *
 * TWO TRAPS, both learned from calibrating the Python auditor — do not "simplify" them away:
 *
 *   1. The assertion vocabulary is bigger than `expect()`. Testing Library's `getByRole` /
 *      `findByText` THROW when the element is missing, so they are assertions, and they are
 *      the only assertion in plenty of render tests. Local helper functions that wrap
 *      `expect` are assertions too. Omit either and a large slice of the suite reads as
 *      assertion-free — that is a detector bug, not a finding.
 *   2. The allowlist key MUST include the category. An entry keyed only by test would
 *      exempt that test from every detector at once, which is how one `mock-heavy`
 *      exemption silently granted a `no-assertion` one in the Python version.
 */

import { main } from './frontend-test-audit/cli.mjs';

process.exit(main(process.argv));
