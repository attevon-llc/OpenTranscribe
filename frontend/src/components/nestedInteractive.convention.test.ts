/**
 * axe `nested-interactive`: an element whose ARIA role presents its children as flat text
 * (button, link, tab, checkbox, switch, radio, option, menuitem*) must not contain another
 * focusable control. Screen readers drop the inner control, and keyboard / voice users
 * cannot reach it as a separate target. The settings modal failed the e2e axe scan on this
 * (a role="button" stat card holding an InfoTip button).
 *
 * The fix pattern is the "stretched link": the card is a plain element, its real <button>
 * has an `::after` covering the card, and the inner controls sit above it with z-index.
 *
 * eslint-plugin-svelte / svelte-check have no rule for nesting (svelte's a11y checks look at
 * one element at a time), so this walks the compiled AST of every component.
 * Dynamic children inside {#if}/{#each} blocks are included; components (`<InfoTip>`) are not
 * resolved, so a component that renders a button internally is caught only by the e2e axe scan.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';
import { parse } from 'svelte/compiler';

const SRC = resolve(__dirname, '..');

/** Roles whose children are presentational in the ARIA spec. */
const FLAT_ROLES = new Set([
  'button',
  'link',
  'tab',
  'checkbox',
  'switch',
  'radio',
  'option',
  'menuitem',
  'menuitemcheckbox',
  'menuitemradio',
  'img',
  'progressbar',
  'meter',
]);

/** Subtrees skipped, with the reason. Not an a11y allowlist: the owning lane is mid-edit. */
const PENDING_OWNER = ['components/speakers/'];

type Node = {
  type: string;
  name?: string;
  attributes?: Attr[];
  fragment?: { nodes: Node[] };
  [k: string]: unknown;
};
type Attr = {
  type: string;
  name: string;
  value: true | { type: string; data?: string }[] | { type: string; data?: string };
};

function attr(node: Node, name: string): Attr | undefined {
  return node.attributes?.find((a) => a.type === 'Attribute' && a.name === name);
}

function staticText(a: Attr | undefined): string | undefined {
  if (!a || a.value === true) return undefined;
  const parts = Array.isArray(a.value) ? a.value : [a.value];
  return parts.every((p) => p.type === 'Text') ? parts.map((p) => p.data).join('') : undefined;
}

function isFocusable(node: Node): boolean {
  if (node.type !== 'RegularElement') return false;
  const tabindex = staticText(attr(node, 'tabindex'));
  if (tabindex !== undefined && tabindex !== '-1') return true;
  if (['button', 'select', 'textarea'].includes(node.name!)) return true;
  if (node.name === 'a') return attr(node, 'href') !== undefined;
  if (node.name === 'input') {
    if (staticText(attr(node, 'type')) === 'hidden' || attr(node, 'hidden')) return false;
    // display:none removes it from the tab order and the accessibility tree.
    return !/display:\s*none/.test(staticText(attr(node, 'style')) ?? '');
  }
  return false;
}

function children(node: Node): Node[] {
  const out: Node[] = [];
  for (const key of [
    'fragment',
    'consequent',
    'alternate',
    'body',
    'fallback',
    'pending',
    'then',
    'catch',
  ]) {
    const f = node[key] as { nodes?: Node[] } | undefined;
    if (f?.nodes) out.push(...f.nodes);
  }
  return out;
}

function findInner(node: Node, hits: string[]): void {
  for (const child of children(node)) {
    if (isFocusable(child)) hits.push(child.name!);
    findInner(child, hits);
  }
}

function scan(node: Node, found: string[]): void {
  if (node.type === 'RegularElement') {
    const role = staticText(attr(node, 'role'));
    const native = node.name === 'button' || (node.name === 'a' && attr(node, 'href'));
    if ((role && FLAT_ROLES.has(role)) || native) {
      const hits: string[] = [];
      findInner(node, hits);
      if (hits.length)
        found.push(
          `<${node.name}${role ? ` role="${role}"` : ''}> contains <${hits.join('>, <')}>`
        );
    }
  }
  for (const child of children(node)) scan(child, found);
}

function scanRoot(ast: { fragment: { nodes: Node[] } }, found: string[]): void {
  for (const n of ast.fragment.nodes) scan(n, found);
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (full.endsWith('.svelte')) out.push(full);
  }
  return out;
}

describe('nested interactive controls (axe nested-interactive)', () => {
  it('detects a button inside role="button" (must-fire control)', () => {
    const ast = parse('<div role="button" tabindex="0"><button>x</button></div>', {
      modern: true,
    }) as unknown as { fragment: { nodes: Node[] } };
    const found: string[] = [];
    scanRoot(ast, found);
    expect(found).toHaveLength(1);
  });

  it('accepts a stretched-button card (must-stay-clean control)', () => {
    const ast = parse('<div class="card"><button class="open">x</button><button>y</button></div>', {
      modern: true,
    }) as unknown as { fragment: { nodes: Node[] } };
    const found: string[] = [];
    scanRoot(ast, found);
    expect(found).toEqual([]);
  });

  it('no component nests a focusable control inside a role that flattens its children', () => {
    const problems: string[] = [];
    for (const file of walk(SRC)) {
      const rel = relative(SRC, file);
      if (PENDING_OWNER.some((p) => rel.startsWith(p))) continue;
      let ast;
      try {
        ast = parse(readFileSync(file, 'utf8'), { modern: true }) as unknown as {
          fragment: { nodes: Node[] };
        };
      } catch {
        continue;
      }
      const found: string[] = [];
      scanRoot(ast, found);
      problems.push(...found.map((f) => `${rel}: ${f}`));
    }
    expect(problems).toEqual([]);
  }, 60_000);
});
