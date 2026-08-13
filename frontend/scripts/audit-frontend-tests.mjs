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

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, resolve } from 'node:path';
import ts from 'typescript';

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(here, '..');

const ALLOWLIST_PATH = join(frontendRoot, 'test-audit-allowlist.txt');

/** `vi.*` / `mock*` references above this in one test mean the test is mostly scaffolding. */
const MAX_MOCK_REFS = 14;

export const CATEGORIES = [
  'only-leak',
  'skipped-test',
  'no-assertion',
  'unfalsifiable',
  'weak-only',
  'conditional-only',
  'conditional-skip',
  'floating-async-assertion',
  'mock-heavy',
  'mock-only',
];

const SUITE_NAMES = new Set(['describe', 'suite']);
const TEST_NAMES = new Set(['it', 'test', 'bench']);

/** Matchers that pass on almost any value. Weak as a test's SOLE assertion. */
const WEAK_MATCHERS = new Set([
  'toBeDefined',
  'toBeTruthy',
  'toBeFalsy',
  'toBeNull',
  'toBeUndefined',
  'toThrow',
  'toThrowError',
]);

/** Matchers that only prove a mock was invoked. */
const CALL_MATCHERS = new Set([
  'toHaveBeenCalled',
  'toHaveBeenCalledOnce',
  'toHaveBeenCalledTimes',
  'toHaveBeenCalledWith',
  'toHaveBeenLastCalledWith',
  'toHaveBeenNthCalledWith',
  'toHaveBeenCalledExactlyOnceWith',
  'toHaveReturned',
  'toHaveReturnedTimes',
]);

const MOCK_HELPERS = new Set([
  'fn',
  'mock',
  'spyOn',
  'doMock',
  'mocked',
  'stubGlobal',
  'mockReturnValue',
  'mockReturnValueOnce',
  'mockResolvedValue',
  'mockResolvedValueOnce',
  'mockRejectedValue',
  'mockRejectedValueOnce',
  'mockImplementation',
  'mockImplementationOnce',
  'mockClear',
  'mockReset',
  'mockRestore',
]);

/**
 * Testing Library queries that throw on failure, so they assert. `queryBy*` deliberately
 * does NOT throw — it returns null — and is excluded.
 */
const THROWING_QUERY = /^(get|find)(All)?By[A-Z]/;

/**
 * `expect.arrayContaining(...)` is an ARGUMENT to a matcher, not an assertion head. Treat it
 * as one and the parent-climb walks out of the real chain and reports a nonsense matcher
 * (calibration bug: `expect(titles).toEqual(expect.arrayContaining([...]))` was reported as
 * `expect([...]).null()` and counted as unfalsifiable because its args are all literals).
 * `expect.soft(x)` IS a head, so it is deliberately absent from this set.
 */
const EXPECT_UTILITIES = new Set([
  'arrayContaining',
  'objectContaining',
  'stringContaining',
  'stringMatching',
  'closeTo',
  'any',
  'anything',
  'assertions',
  'hasAssertions',
  'unreachable',
  'extend',
  'addSnapshotSerializer',
]);

// ---------------------------------------------------------------------------- AST helpers

/** Name of a called callee, plus the `.skip`/`.only`/`.each` modifiers applied to it. */
function calleeInfo(expr) {
  const modifiers = [];
  let node = expr;
  // Unwrap `it.each([...])('name', fn)` and `it.for(...)(...)`.
  while (ts.isCallExpression(node)) node = node.expression;
  while (ts.isPropertyAccessExpression(node)) {
    modifiers.unshift(node.name.text);
    node = node.expression;
  }
  if (ts.isIdentifier(node)) return { base: node.text, modifiers };
  return { base: null, modifiers };
}

function literalText(node) {
  if (!node) return null;
  if (ts.isStringLiteralLike(node)) return node.text;
  if (ts.isTemplateExpression(node)) return node.getText();
  return null;
}

/** True for a node that is a compile-time constant, so an assertion on it cannot fail. */
function isConstantExpression(node) {
  if (!node) return false;
  switch (node.kind) {
    case ts.SyntaxKind.TrueKeyword:
    case ts.SyntaxKind.FalseKeyword:
    case ts.SyntaxKind.NullKeyword:
    case ts.SyntaxKind.NumericLiteral:
    case ts.SyntaxKind.StringLiteral:
    case ts.SyntaxKind.NoSubstitutionTemplateLiteral:
      return true;
    case ts.SyntaxKind.Identifier:
      return node.text === 'undefined' || node.text === 'NaN';
    case ts.SyntaxKind.PrefixUnaryExpression:
      return isConstantExpression(node.operand);
    case ts.SyntaxKind.ArrayLiteralExpression:
      return node.elements.every(isConstantExpression);
    case ts.SyntaxKind.ObjectLiteralExpression:
      return node.properties.every(
        (p) => ts.isPropertyAssignment(p) && isConstantExpression(p.initializer)
      );
    default:
      return false;
  }
}

function walk(node, visit) {
  visit(node);
  node.forEachChild((child) => walk(child, visit));
}

/** Every function-like node inside `node`, so we can skip nested test bodies. */
function isFunctionLike(node) {
  return (
    ts.isArrowFunction(node) ||
    ts.isFunctionExpression(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isMethodDeclaration(node)
  );
}

/**
 * Describe/test callbacks nested inside `body` — these belong to a different test and must
 * not be attributed to this one.
 */
function nestedTestCallbacks(body) {
  const out = new Set();
  walk(body, (node) => {
    if (!ts.isCallExpression(node)) return;
    const { base } = calleeInfo(node.expression);
    if (!base || (!TEST_NAMES.has(base) && !SUITE_NAMES.has(base))) return;
    for (const arg of node.arguments) if (isFunctionLike(arg)) out.add(arg);
  });
  return out;
}

/** Walk `body` but stop at any node in `stop` (used to exclude nested test callbacks). */
function walkExcluding(body, stop, visit) {
  const recurse = (node) => {
    if (node !== body && stop.has(node)) return;
    visit(node);
    node.forEachChild(recurse);
  };
  recurse(body);
}

// ------------------------------------------------------------------- assertion extraction

/**
 * Resolve the full matcher chain hanging off an `expect(...)` call.
 * `await expect(p).rejects.toThrow(E)` -> { matcher: 'toThrow', negated: false,
 * asyncChain: 'rejects', awaited: true, args: [E] }.
 */
function matcherChain(expectCall) {
  const props = [];
  let node = expectCall;
  let finalCall = null;
  for (;;) {
    const parent = node.parent;
    if (!parent) break;
    if (ts.isPropertyAccessExpression(parent) && parent.expression === node) {
      props.push(parent.name.text);
      node = parent;
      continue;
    }
    if (ts.isCallExpression(parent) && parent.expression === node) {
      finalCall = parent;
      node = parent;
      break;
    }
    break;
  }
  const modifiers = new Set(['not', 'resolves', 'rejects', 'soft']);
  const matcher = [...props].reverse().find((p) => !modifiers.has(p)) ?? null;
  let awaited = false;
  let cursor = finalCall ?? node;
  for (let depth = 0; depth < 4 && cursor?.parent; depth += 1) {
    const parent = cursor.parent;
    if (ts.isAwaitExpression(parent) || ts.isReturnStatement(parent)) {
      awaited = true;
      break;
    }
    // `await Promise.all([expect(a).rejects.toThrow(), ...])`
    if (ts.isArrayLiteralExpression(parent) || ts.isCallExpression(parent)) {
      cursor = parent;
      continue;
    }
    break;
  }
  return {
    matcher,
    negated: props.includes('not'),
    asyncChain: props.includes('rejects')
      ? 'rejects'
      : props.includes('resolves')
        ? 'resolves'
        : null,
    awaited,
    args: finalCall ? [...finalCall.arguments] : [],
    node: finalCall ?? expectCall,
  };
}

function subjectText(expectCall) {
  return expectCall.arguments.length ? expectCall.arguments[0].getText() : '';
}

const MOCK_SUBJECT = /(^|[^A-Za-z])(mock|spy|stub)|Mock|Spy|Stub|\.mock\.|vi\.fn|Called/;

/** Assertion subjects that reflect what the user would see, not mock bookkeeping. */
const DOM_SUBJECT =
  /(screen\.|container|getBy|findBy|queryBy|getAllBy|findAllBy|queryAllBy|\.textContent|\.innerHTML|document\.|\.value\b|baseElement|\.outerHTML)/;

// ------------------------------------------------------------------------ guard detection

/**
 * Classify how `node` is guarded relative to `body`:
 *   'if-no-else'   — inside the then-branch of an `if` with no else
 *   'if-else-skip' — inside the then-branch of an `if` whose else only skips/returns
 *   null           — reached unconditionally
 */
function guardKind(node, body) {
  let kind = null;
  let cursor = node;
  while (cursor && cursor !== body) {
    const parent = cursor.parent;
    if (!parent) break;
    if (ts.isIfStatement(parent)) {
      if (parent.thenStatement === cursor) {
        if (!parent.elseStatement) return 'if-no-else';
        if (branchOnlySkips(parent.elseStatement)) kind = 'if-else-skip';
      } else if (parent.elseStatement === cursor && branchOnlySkips(parent.thenStatement)) {
        // `if (!supported) { ctx.skip(); } else { expect(...) }` — the assertion is in the
        // ELSE branch. Checking only `thenStatement` missed the shape people actually write
        // (caught by --selftest, which is why that flag exists).
        kind = 'if-else-skip';
      }
    }
    cursor = parent;
  }
  return kind;
}

/** True when a branch does nothing but skip or bail out. */
function branchOnlySkips(branch) {
  const statements = ts.isBlock(branch) ? [...branch.statements] : [branch];
  if (!statements.length) return true;
  return statements.every((stmt) => {
    if (ts.isReturnStatement(stmt) && !stmt.expression) return true;
    if (!ts.isExpressionStatement(stmt)) return false;
    let expr = stmt.expression;
    if (ts.isAwaitExpression(expr)) expr = expr.expression;
    if (!ts.isCallExpression(expr)) return false;
    const { base, modifiers } = calleeInfo(expr.expression);
    return modifiers.includes('skip') || base === 'skip';
  });
}

/**
 * An early-return guard before the first assertion: `if (!thing) return;` then assert.
 * Silently degrades the test to a no-op exactly like a conditional-only assertion.
 */
function earlyReturnGuard(body, firstAssertion) {
  if (!firstAssertion || !ts.isBlock(body)) return null;
  // `.getStart()`, never `.pos`: `pos` includes leading trivia and therefore equals the
  // previous statement's `end`, so a `stmt.end >= firstAssertion.pos` comparison bailed out
  // on the very first statement and the detector never fired (caught by --selftest).
  const assertionStart = firstAssertion.getStart();
  for (const stmt of body.statements) {
    if (stmt.getStart() >= assertionStart) break;
    if (ts.isIfStatement(stmt) && !stmt.elseStatement && branchOnlySkips(stmt.thenStatement)) {
      return stmt.expression.getText();
    }
  }
  return null;
}

// -------------------------------------------------------------------------- file scanning

/** Names of file-local functions whose body contains an assertion — calls to them assert. */
function assertingHelpers(sourceFile) {
  const names = new Set();
  const containsExpect = (node) => {
    let found = false;
    walk(node, (n) => {
      if (found || !ts.isCallExpression(n)) return;
      const { base, modifiers } = calleeInfo(n.expression);
      if (base === 'expect' || base === 'expectTypeOf' || base === 'assert') found = true;
      if (base === 'expect' && modifiers.length) found = true;
    });
    return found;
  };
  walk(sourceFile, (node) => {
    if (ts.isFunctionDeclaration(node) && node.name && node.body && containsExpect(node.body)) {
      names.add(node.name.text);
    }
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      isFunctionLike(node.initializer) &&
      containsExpect(node.initializer)
    ) {
      names.add(node.name.text);
    }
  });
  return names;
}

function analyseTest({ body, helpers, sourceFile }) {
  const stop = nestedTestCallbacks(body);
  const assertions = [];
  const throwingQueries = [];
  const helperCalls = [];
  let mockRefs = 0;

  walkExcluding(body, stop, (node) => {
    if (!ts.isCallExpression(node)) return;
    const { base, modifiers } = calleeInfo(node.expression);

    if (base === 'expect' || base === 'expectTypeOf') {
      // `expect.arrayContaining(...)` etc. are matcher ARGUMENTS, not assertion heads.
      if (modifiers.some((m) => EXPECT_UTILITIES.has(m))) return;
      // Only the head of the chain: `expect(x).not.toBe(y)` has one expect() call.
      assertions.push({ call: node, ...matcherChain(node) });
      return;
    }
    if (base === 'assert') {
      assertions.push({
        call: node,
        matcher: 'assert',
        negated: false,
        asyncChain: null,
        awaited: true,
        args: [],
        node,
      });
      return;
    }
    if (base && helpers.has(base)) helperCalls.push(node);

    const tail = modifiers[modifiers.length - 1];
    if (base === 'vi' && tail && MOCK_HELPERS.has(tail)) mockRefs += 1;
    else if (tail && MOCK_HELPERS.has(tail)) mockRefs += 1;

    // `screen.getByRole(...)` / bare `getByText(...)` throw when absent, so they assert.
    const queryName = tail ?? base;
    if (queryName && THROWING_QUERY.test(queryName)) throwingQueries.push(node);
  });

  return { assertions, throwingQueries, helperCalls, mockRefs };
}

/**
 * Scan test source text. Split out from `scanFile` so `--selftest` can drive every detector
 * from in-memory fixtures — a detector that cannot fire is the same sin this script exists to
 * catch, and fixtures on disk would be collected by vitest and linted as real tests.
 */
function scanSource(text, rel) {
  const sourceFile = ts.createSourceFile(rel, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const helpers = assertingHelpers(sourceFile);
  const findings = [];
  let testsSeen = 0;
  const add = (category, node, name, detail) =>
    findings.push({
      category,
      path: rel,
      line: sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1,
      test: name,
      detail,
    });

  /** Recurse through describe/it, carrying the enclosing suite names. */
  const visit = (node, prefix) => {
    if (ts.isCallExpression(node)) {
      const { base, modifiers } = calleeInfo(node.expression);
      const title = literalText(node.arguments[0]);
      const isSuite = base && SUITE_NAMES.has(base);
      const isTest = base && TEST_NAMES.has(base);

      if (isSuite || isTest) {
        const name = [...prefix, title ?? '<dynamic>'].join(' > ');
        if (modifiers.includes('only')) {
          add('only-leak', node, name, `${base}.only — disables every other test in the file`);
        }
        if (modifiers.includes('skip') || modifiers.includes('todo')) {
          const which = modifiers.includes('todo') ? 'todo' : 'skip';
          add('skipped-test', node, name, `${base}.${which}`);
        }
        const callback = node.arguments.find(isFunctionLike);
        if (isSuite) {
          if (callback?.body) visit_children(callback.body, [...prefix, title ?? '<dynamic>']);
          return;
        }
        testsSeen += 1;
        if (callback?.body) checkTest(node, callback.body, name);
        return;
      }
    }
    node.forEachChild((child) => visit(child, prefix));
  };

  const visit_children = (body, prefix) => body.forEachChild((child) => visit(child, prefix));

  const checkTest = (testNode, body, name) => {
    const { assertions, throwingQueries, helperCalls, mockRefs } = analyseTest({
      body,
      helpers,
      sourceFile,
    });

    for (const a of assertions) {
      const subject = a.call.arguments[0];
      const constantSubject = isConstantExpression(subject);
      const constantArgs = a.args.every(isConstantExpression);
      if (constantSubject && constantArgs) {
        add(
          'unfalsifiable',
          a.node,
          name,
          `expect(${subjectText(a.call)}).${a.negated ? 'not.' : ''}${a.matcher}(${a.args
            .map((x) => x.getText())
            .join(', ')}) — both sides constant`
        );
      }
      if (a.asyncChain && !a.awaited) {
        add(
          'floating-async-assertion',
          a.node,
          name,
          `.${a.asyncChain}.${a.matcher} is not awaited — resolves after the test passes`
        );
      }
    }

    const total = assertions.length + throwingQueries.length + helperCalls.length;

    // weak-only: the test's entire assertion budget is spent on matchers that pass on
    // nearly any value. A throwing query or helper call is real evidence, so its presence
    // clears the finding.
    if (assertions.length && !throwingQueries.length && !helperCalls.length) {
      const weak = assertions.filter((a) => {
        if (!a.matcher || !WEAK_MATCHERS.has(a.matcher)) return false;
        // `not.toThrow()` is weak; `toThrow()` asserts a real failure mode.
        if (a.matcher === 'toThrow' || a.matcher === 'toThrowError') return a.negated;
        // `not.toBeNull()`/`not.toBeUndefined()` are weak; the positive forms are exact.
        if (a.matcher === 'toBeNull' || a.matcher === 'toBeUndefined') return a.negated;
        return !a.negated;
      });
      if (weak.length === assertions.length) {
        add(
          'weak-only',
          weak[0].node,
          name,
          `${assertions.length} assertion(s), all weak: ${[
            ...new Set(weak.map((a) => (a.negated ? `not.${a.matcher}` : a.matcher))),
          ].join(', ')}`
        );
      }
    }

    if (total === 0) {
      add('no-assertion', testNode, name, 'no expect/assert/throwing query/assert-helper');
    }

    // conditional-only / conditional-skip
    const evidence = [...assertions.map((a) => a.node), ...throwingQueries, ...helperCalls];
    if (evidence.length) {
      const guards = evidence.map((n) => guardKind(n, body));
      if (guards.every((g) => g === 'if-no-else')) {
        add(
          'conditional-only',
          evidence[0],
          name,
          `${evidence.length} assertion(s), all inside if-without-else`
        );
      } else if (guards.every((g) => g !== null)) {
        add(
          'conditional-skip',
          evidence[0],
          name,
          `${evidence.length} assertion(s), all behind a guard whose else only skips`
        );
      } else {
        const guard = earlyReturnGuard(body, evidence[0]);
        if (guard) {
          add('conditional-skip', evidence[0], name, `early-return guard \`if (${guard})\``);
        }
      }
    }

    if (mockRefs >= MAX_MOCK_REFS) {
      add('mock-heavy', testNode, name, `${mockRefs} vi.*/mock* references`);
    }

    // mock-only: proves the mock was called, never that anything rendered or returned.
    if (assertions.length && !throwingQueries.length && !helperCalls.length) {
      const allCallMatchers = assertions.every((a) => a.matcher && CALL_MATCHERS.has(a.matcher));
      const anyDom = assertions.some((a) => DOM_SUBJECT.test(subjectText(a.call)));
      const allMockSubjects = assertions.every((a) => MOCK_SUBJECT.test(subjectText(a.call)));
      if (allCallMatchers && allMockSubjects && !anyDom) {
        add(
          'mock-only',
          assertions[0].node,
          name,
          `${assertions.length} assertion(s), all toHaveBeenCalled* on a mock`
        );
      }
    }
  };

  visit_children(sourceFile, []);
  return { findings, testsSeen };
}

function scanFile(path, root) {
  return scanSource(readFileSync(path, 'utf8'), relative(root, path));
}

// ------------------------------------------------------------------------------ allowlist

/** Reason prefix marking an entry as DEFERRED WORK rather than an accepted pattern. */
const BACKLOG_PREFIX = 'BACKLOG';

/**
 * `key -> [reason, ...]`, one entry per LINE. An array, not a string: one line buys one
 * finding, so duplicate keys encode a count rather than being an error to reject.
 */
export function loadAllowlist(text) {
  const entries = new Map();
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const hash = line.indexOf('#');
    const key = (hash === -1 ? line : line.slice(0, hash)).trim();
    const reason = hash === -1 ? '' : line.slice(hash + 1).trim();
    if (!key) continue;
    if (!entries.has(key)) entries.set(key, []);
    entries.get(key).push(reason || 'no reason given');
  }
  return entries;
}

const keyOf = (f) => `${f.path}::${f.test}::${f.category}`;

/**
 * Split findings by allowlist coverage, pairing one line to one finding.
 *
 * Returns `{ open, backlog, accepted, stale }`. `stale` names every key holding more lines
 * than the tree still produces — the key vanished entirely, or it was PARTIALLY fixed. The
 * second case is the one a set difference is blind to, and it is the common one: a test with
 * three offending assertions gets three lines, and repairing two of them must not leave the
 * third quietly exempt.
 */
export function applyAllowlist(findings, allowed) {
  const byKey = new Map();
  for (const f of findings) {
    const key = keyOf(f);
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(f);
  }

  const open = [];
  const backlog = [];
  let accepted = 0;
  for (const [key, group] of byKey) {
    const reasons = allowed.get(key) ?? [];
    group.slice(0, reasons.length).forEach((f, i) => {
      if (reasons[i].startsWith(BACKLOG_PREFIX)) backlog.push(f);
      else accepted += 1;
    });
    open.push(...group.slice(reasons.length));
  }

  const stale = [];
  for (const [key, reasons] of [...allowed].sort(([a], [b]) => (a < b ? -1 : 1))) {
    const live = byKey.get(key)?.length ?? 0;
    if (live >= reasons.length) continue;
    stale.push(
      live === 0
        ? `${key}  (${reasons.length} entr${reasons.length === 1 ? 'y' : 'ies'}, 0 findings)`
        : `${key}  (${reasons.length} entries, ${live} finding(s) — delete ${
            reasons.length - live
          })`
    );
  }
  return { open, backlog, accepted, stale };
}

function collectTestFiles(dir) {
  if (!statSync(dir).isDirectory()) return [dir];
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.')) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...collectTestFiles(full));
    else if (/\.(test|spec)\.(ts|js)$/.test(entry)) out.push(full);
  }
  return out.sort();
}

// ------------------------------------------------------------------------------- self-test

/**
 * Each case is source that MUST produce the named category, plus `clean` cases that must
 * produce nothing. This is the auditor auditing itself: a detector that silently stops
 * matching (a refactor, a vitest API change) is indistinguishable from a clean suite, which
 * is exactly the failure mode this whole script exists to prevent.
 */
const SELFTEST_CASES = [
  ['only-leak', "describe('d', () => { it.only('a', () => { expect(1).toBe(2); }); });"],
  ['skipped-test', "describe('d', () => { it.skip('a', () => { expect(1).toBe(2); }); });"],
  ['skipped-test', "describe('d', () => { it.todo('a'); });"],
  ['no-assertion', "describe('d', () => { it('a', () => { const x = compute(); }); });"],
  ['unfalsifiable', "describe('d', () => { it('a', () => { expect(true).toBe(true); }); });"],
  ['unfalsifiable', "describe('d', () => { it('a', () => { expect(1).toBe(1); }); });"],
  ['weak-only', "describe('d', () => { it('a', () => { expect(thing()).toBeDefined(); }); });"],
  ['weak-only', "describe('d', () => { it('a', () => { expect(() => f()).not.toThrow(); }); });"],
  [
    'conditional-only',
    "describe('d', () => { it('a', () => { if (feature) { expect(x).toBe(1); } }); });",
  ],
  [
    'conditional-skip',
    "describe('d', () => { it('a', (ctx) => { if (!feature) { ctx.skip(); } else { expect(x).toBe(1); } }); });",
  ],
  [
    'conditional-skip',
    "describe('d', () => { it('a', () => { if (!feature) return; expect(x).toBe(1); }); });",
  ],
  [
    'floating-async-assertion',
    "describe('d', () => { it('a', async () => { expect(p()).rejects.toThrow(); }); });",
  ],
  [
    'mock-heavy',
    `describe('d', () => { it('a', () => {
      const m = vi.fn(); vi.spyOn(o, 'x'); vi.stubGlobal('fetch', vi.fn());
      m.mockReturnValue(1); m.mockResolvedValue(2); m.mockImplementation(() => 3);
      m.mockClear(); m.mockReset(); m.mockRestore(); m.mockReturnValueOnce(4);
      m.mockResolvedValueOnce(5); m.mockRejectedValue(6); m.mockRejectedValueOnce(7);
      m.mockImplementationOnce(() => 8);
      expect(render(C).container.textContent).toBe('x');
    }); });`,
  ],
  [
    'mock-only',
    "describe('d', () => { it('a', () => { doThing(); expect(mockPost).toHaveBeenCalledWith('/x'); }); });",
  ],
];

/** Sources that must produce NO finding — the false-positive half of the calibration. */
const SELFTEST_CLEAN = [
  // Trap 1: throwing Testing Library queries ARE assertions.
  "describe('d', () => { it('a', () => { render(C); screen.getByRole('button', { name: 'Save' }); }); });",
  // Trap 1: a local helper that wraps expect() is an assertion.
  `function expectRow(el, label) { expect(el.textContent).toBe(label); }
   describe('d', () => { it('a', () => { expectRow(render(C).container, 'x'); }); });`,
  // `expect.arrayContaining` is a matcher ARGUMENT, not an assertion head.
  "describe('d', () => { it('a', () => { expect(list).toEqual(expect.arrayContaining(['a'])); }); });",
  // An awaited rejects chain is fine.
  "describe('d', () => { it('a', async () => { await expect(p()).rejects.toThrow(); }); });",
  // A call assertion alongside a real output assertion is not mock-only.
  `describe('d', () => { it('a', () => {
     expect(mockPost).toHaveBeenCalledWith('/x'); expect(result.name).toBe('ops');
   }); });`,
  // Guarded assertion plus an unguarded one is not conditional-only.
  "describe('d', () => { it('a', () => { expect(x).toBe(1); if (y) { expect(y).toBe(2); } }); });",
  // `toThrow()` (positive) is a real failure-mode assertion, not weak.
  "describe('d', () => { it('a', () => { expect(() => f()).toThrow('boom'); }); });",
];

/**
 * The allowlist rails, self-tested for the same reason the detectors are: a stale check that
 * stops noticing reports nothing, which reads exactly like a clean allowlist. Each case names
 * what must happen and what must NOT.
 */
const ALLOWLIST_CASES = [
  [
    'one line covers one finding',
    () => {
      const f = { path: 'a.test.ts', test: 'x', category: 'weak-only', line: 1, detail: '' };
      const r = applyAllowlist([f], loadAllowlist('a.test.ts::x::weak-only  # reason'));
      return r.open.length === 0 && r.accepted === 1 && r.stale.length === 0;
    },
  ],
  [
    'one line does NOT cover three findings',
    () => {
      const f = (line) => ({
        path: 'a.test.ts',
        test: 'x',
        category: 'weak-only',
        line,
        detail: '',
      });
      const r = applyAllowlist([f(1), f(2), f(3)], loadAllowlist('a.test.ts::x::weak-only  # r'));
      return r.open.length === 2;
    },
  ],
  [
    'an entry whose finding is gone is STALE',
    () => applyAllowlist([], loadAllowlist('a.test.ts::x::weak-only  # r')).stale.length === 1,
  ],
  [
    'a PARTIAL fix leaves surplus lines, and they are STALE',
    () => {
      const f = { path: 'a.test.ts', test: 'x', category: 'weak-only', line: 1, detail: '' };
      const list = loadAllowlist(
        'a.test.ts::x::weak-only  # r\na.test.ts::x::weak-only  # r\na.test.ts::x::weak-only  # r'
      );
      const r = applyAllowlist([f], list);
      return r.open.length === 0 && r.stale.length === 1 && r.stale[0].includes('delete 2');
    },
  ],
  [
    'a BACKLOG reason is counted as deferred work, not accepted',
    () => {
      const f = { path: 'a.test.ts', test: 'x', category: 'weak-only', line: 1, detail: '' };
      const r = applyAllowlist([f], loadAllowlist('a.test.ts::x::weak-only  # BACKLOG(#431): tbd'));
      return r.backlog.length === 1 && r.accepted === 0 && r.open.length === 0;
    },
  ],
  [
    'a comment line is not an entry',
    () => loadAllowlist('# a.test.ts::x::weak-only  # r\n\n').size === 0,
  ],
];

function runSelfTest() {
  let failures = 0;
  console.log('\n\x1b[1maudit-frontend-tests self-test\x1b[0m\n');
  for (const [label, check] of ALLOWLIST_CASES) {
    let ok = false;
    try {
      ok = check() === true;
    } catch (err) {
      console.log(`      threw: ${err.message}`);
    }
    console.log(`  ${ok ? '\x1b[32m✓' : '\x1b[31m✗'}\x1b[0m allowlist: ${label}`);
    if (!ok) failures += 1;
  }
  for (const [category, source] of SELFTEST_CASES) {
    const { findings } = scanSource(source, 'fixture.test.ts');
    const hit = findings.some((f) => f.category === category);
    console.log(`  ${hit ? '\x1b[32m✓' : '\x1b[31m✗'}\x1b[0m fires ${category}`);
    if (!hit) {
      failures += 1;
      console.log(`      got: ${JSON.stringify(findings.map((f) => f.category))}`);
    }
  }
  for (const [i, source] of SELFTEST_CLEAN.entries()) {
    const { findings } = scanSource(source, 'fixture.test.ts');
    const ok = findings.length === 0;
    console.log(
      `  ${ok ? '\x1b[32m✓' : '\x1b[31m✗'}\x1b[0m clean case ${i + 1} produces no finding`
    );
    if (!ok) {
      failures += 1;
      console.log(
        `      got: ${JSON.stringify(findings.map((f) => `${f.category}: ${f.detail}`))}`
      );
    }
  }
  if (failures) {
    console.log(`\n\x1b[31m${failures} self-test failure(s) — a detector is broken\x1b[0m\n`);
    return 1;
  }
  const total = SELFTEST_CASES.length + SELFTEST_CLEAN.length + ALLOWLIST_CASES.length;
  console.log(`\n\x1b[32mall ${total} self-test cases pass\x1b[0m\n`);
  return 0;
}

// ----------------------------------------------------------------------------------- main

function main(argv) {
  const args = argv.slice(2);
  const flags = new Set(args.filter((a) => a.startsWith('--')));
  const categoryIdx = args.indexOf('--category');
  const category = categoryIdx === -1 ? null : args[categoryIdx + 1];
  const positional = args.filter((a, i) => !a.startsWith('--') && args[i - 1] !== '--category');
  const target = resolve(frontendRoot, positional[0] ?? 'src');

  if (flags.has('--selftest')) return runSelfTest();
  if (category && !CATEGORIES.includes(category)) {
    console.error(`error: unknown category ${category}\n  known: ${CATEGORIES.join(', ')}`);
    return 2;
  }
  if (!existsSync(target)) {
    console.error(`error: ${target} does not exist`);
    return 2;
  }

  const scanRoot = resolve(frontendRoot, 'src');
  const paths = collectTestFiles(target);
  const scans = paths.map((path) => scanFile(path, scanRoot));
  const testsSeen = scans.reduce((n, s) => n + s.testsSeen, 0);
  let findings = scans.flatMap((s) => s.findings);
  if (category) findings = findings.filter((f) => f.category === category);

  const allowed = existsSync(ALLOWLIST_PATH)
    ? loadAllowlist(readFileSync(ALLOWLIST_PATH, 'utf8'))
    : new Map();
  const { open, backlog, accepted, stale: allStale } = applyAllowlist(findings, allowed);

  // Staleness is only meaningful on a FULL scan: a `--category` run or a subdirectory has not
  // looked at the findings the other entries cover, and would report every one of them.
  const fullScan = !category && target === scanRoot;
  const stale = fullScan ? allStale : [];
  const all = CATEGORIES;

  if (flags.has('--json')) {
    console.log(
      JSON.stringify(
        {
          files: paths.length,
          tests_seen: testsSeen,
          total: findings.length,
          accepted,
          backlog: backlog.length,
          unallowlisted: open.length,
          stale_allowlist_entries: stale,
          by_category: open.reduce(
            (acc, f) => ({ ...acc, [f.category]: (acc[f.category] ?? 0) + 1 }),
            {}
          ),
          backlog_by_category: backlog.reduce(
            (acc, f) => ({ ...acc, [f.category]: (acc[f.category] ?? 0) + 1 }),
            {}
          ),
          findings: open,
        },
        null,
        2
      )
    );
    return open.length || stale.length ? 1 : 0;
  }

  console.log(
    `\n\x1b[1m${relative(frontendRoot, target) || '.'}\x1b[0m — ${paths.length} test file(s), ` +
      `${testsSeen} tests, ${findings.length} findings: ${open.length} open, ` +
      `${backlog.length} backlog, ${accepted} accepted\n`
  );
  for (const cat of all) {
    const hits = open.filter((f) => f.category === cat);
    const total = findings.filter((f) => f.category === cat).length;
    const deferred = backlog.filter((f) => f.category === cat).length;
    const colour = hits.length ? '\x1b[31m' : '\x1b[32m';
    console.log(
      `  ${colour}${cat.padEnd(26)}\x1b[0m ${String(hits.length).padStart(
        4
      )} open  (${total} total${deferred ? `, ${deferred} backlog` : ''})`
    );
    const show = flags.has('--list') ? hits : hits.slice(0, 5);
    for (const f of show) console.log(`      ${f.path}:${f.line} ${f.test} — ${f.detail}`);
    if (!flags.has('--list') && hits.length > 5)
      console.log(`      … ${hits.length - 5} more (--list)`);
  }

  if (stale.length) {
    console.log(
      `\n\x1b[31m${stale.length} allowlist key(s) hold more entries than findings:\x1b[0m`
    );
    for (const key of stale.slice(0, 20)) console.log(`  ${key}`);
    if (stale.length > 20) console.log(`  … ${stale.length - 20} more`);
    console.log(`  Delete the surplus lines from ${relative(frontendRoot, ALLOWLIST_PATH)}. One`);
    console.log('  line buys one finding: an exemption must never outlive its subject, and a');
    console.log(
      '  key with no finding left silently pre-exempts the next test to reuse the name.\n'
    );
  }
  if (backlog.length) {
    console.log(
      `\x1b[1;33m${backlog.length} finding(s) are DEFERRED WORK, not accepted patterns.\x1b[0m`
    );
    console.log(
      `  They carry a \`${BACKLOG_PREFIX}\` reason in ${relative(frontendRoot, ALLOWLIST_PATH)}.` +
        ' This gate is green\n  because nothing NEW landed — not because the tree is clean.'
    );
  }
  if (open.length) {
    console.log(`\n\x1b[31m${open.length} findings need a fix or an allowlist entry\x1b[0m`);
    console.log(
      `  allowlist: ${relative(frontendRoot, ALLOWLIST_PATH)}` +
        '  (one "<file>::<full test name>::<category>  # reason" per line)\n'
    );
  }
  if (open.length || stale.length) return 1;
  console.log('\n\x1b[32mno un-allowlisted findings\x1b[0m\n');
  return 0;
}

process.exit(main(process.argv));
