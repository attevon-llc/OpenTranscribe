/** TypeScript-AST helpers and assertion extraction for the frontend test auditor. */

import ts from 'typescript';
import { SUITE_NAMES, TEST_NAMES } from './config.mjs';

// ---------------------------------------------------------------------------- AST helpers

/** Name of a called callee, plus the `.skip`/`.only`/`.each` modifiers applied to it. */
export function calleeInfo(expr) {
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

export function literalText(node) {
  if (!node) return null;
  if (ts.isStringLiteralLike(node)) return node.text;
  if (ts.isTemplateExpression(node)) return node.getText();
  return null;
}

/** True for a node that is a compile-time constant, so an assertion on it cannot fail. */
export function isConstantExpression(node) {
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

export function walk(node, visit) {
  visit(node);
  node.forEachChild((child) => walk(child, visit));
}

/** Every function-like node inside `node`, so we can skip nested test bodies. */
export function isFunctionLike(node) {
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
export function nestedTestCallbacks(body) {
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
export function walkExcluding(body, stop, visit) {
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
export function matcherChain(expectCall) {
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

export function subjectText(expectCall) {
  return expectCall.arguments.length ? expectCall.arguments[0].getText() : '';
}

export const MOCK_SUBJECT = /(^|[^A-Za-z])(mock|spy|stub)|Mock|Spy|Stub|\.mock\.|vi\.fn|Called/;

/** Assertion subjects that reflect what the user would see, not mock bookkeeping. */
export const DOM_SUBJECT =
  /(screen\.|container|getBy|findBy|queryBy|getAllBy|findAllBy|queryAllBy|\.textContent|\.innerHTML|document\.|\.value\b|baseElement|\.outerHTML)/;
