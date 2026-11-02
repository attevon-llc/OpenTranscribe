/** Detect conditional guards and early returns that let a test skip its assertions. */

import ts from 'typescript';
import { calleeInfo } from './ast.mjs';

// ------------------------------------------------------------------------ guard detection

/**
 * Classify how `node` is guarded relative to `body`:
 *   'if-no-else'   — inside the then-branch of an `if` with no else
 *   'if-else-skip' — inside the then-branch of an `if` whose else only skips/returns
 *   null           — reached unconditionally
 */
export function guardKind(node, body) {
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
export function earlyReturnGuard(body, firstAssertion) {
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
