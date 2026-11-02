/** Scan test files: per-test analysis, per-file findings and test-file collection. */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import ts from 'typescript';
import {
  DOM_SUBJECT,
  MOCK_SUBJECT,
  calleeInfo,
  isConstantExpression,
  isFunctionLike,
  literalText,
  matcherChain,
  nestedTestCallbacks,
  subjectText,
  walk,
  walkExcluding,
} from './ast.mjs';
import {
  CALL_MATCHERS,
  EXPECT_UTILITIES,
  MAX_MOCK_REFS,
  MOCK_HELPERS,
  SUITE_NAMES,
  TEST_NAMES,
  THROWING_QUERY,
  WEAK_MATCHERS,
} from './config.mjs';
import { earlyReturnGuard, guardKind } from './guards.mjs';

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
export function scanSource(text, rel) {
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

export function scanFile(path, root) {
  return scanSource(readFileSync(path, 'utf8'), relative(root, path));
}

export function collectTestFiles(dir) {
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
