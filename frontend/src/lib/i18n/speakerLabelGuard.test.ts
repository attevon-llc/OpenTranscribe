import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { parse } from 'svelte/compiler';

// API speaker fields that can carry the diarizer placeholder (SPEAKER_NN). Rendering one
// of them in markup must go through translateSpeakerLabel (or a local helper wrapping it).
const FIELDS = new Set([
  'speakers',
  'primary_speakers',
  'speaker',
  'speaker_label',
  'speaker_name',
]);
const BARE = new Set(['speakers', 'primary_speakers', 'visibleSpeakers']);
// getSpeakerColor only keys a colour from the label; it renders no text.
const TRANSLATORS = new Set(['translateSpeakerLabel', 'speakerNames', 'getSpeakerColor', '$t']);

type Node = { type?: string; start?: number; end?: number; [k: string]: unknown };

function svelteFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return svelteFiles(p);
    return p.endsWith('.svelte') ? [p] : [];
  });
}

/** Template expressions (text `{...}` and attribute values) that read a speaker field untranslated. */
export function findRawSpeakerExpressions(src: string): string[] {
  const ast = parse(src, { modern: true }) as unknown as { fragment: Node };
  const out: string[] = [];
  const text = (n: Node) => src.slice(n.start as number, n.end as number);

  // Reads the expression's AST (not its text) so string literals such as i18n keys never match.
  // A call's arguments are a transformation (count, lookup, translate), so only its callee is
  // inspected; `x.speakers.total` is a property of the value, not the label itself, but
  // `x.speakers.join(', ')` renders it.
  function isLabelRead(
    n: Node & { name?: string; property?: Node; computed?: boolean },
    tainted: Set<string>
  ) {
    if (n.type === 'Identifier') return BARE.has(n.name as string) || tainted.has(n.name as string);
    return (
      n.type === 'MemberExpression' &&
      !n.computed &&
      FIELDS.has((n.property as Node).name as string)
    );
  }
  function reads(node: unknown, tainted: Set<string>, callee = false): boolean {
    if (Array.isArray(node)) return node.some((c) => reads(c, tainted));
    if (!node || typeof node !== 'object') return false;
    const n = node as Node & { callee?: Node; object?: Node; name?: string };
    if (n.type === 'CallExpression') {
      const c = n.callee as Node & { name?: string };
      if (c.type === 'Identifier' && TRANSLATORS.has(c.name as string)) return false;
      return reads(c, tainted, true);
    }
    if (n.type === 'MemberExpression' && !callee && n.object && isLabelRead(n.object, tainted)) {
      return false;
    }
    if (isLabelRead(n, tainted)) return true;
    return Object.entries(n).some(([k, v]) => k !== 'arguments' && reads(v, tainted));
  }

  // Only attributes that render user-visible text; bindings/handlers do not.
  const renders = (n: Node) =>
    n.type === 'ExpressionTag' || /^(title|aria-label|alt|placeholder)=/.test(text(n));

  function eachScope(n: Node, tainted: Set<string>): Set<string> {
    const ctx = n.context as (Node & { name?: string }) | null;
    if (n.type !== 'EachBlock' || ctx?.type !== 'Identifier' || !reads(n.expression, tainted)) {
      return tainted;
    }
    return new Set(tainted).add(ctx.name as string);
  }

  function walk(node: unknown, tainted: Set<string>): void {
    if (Array.isArray(node)) return node.forEach((c) => walk(c, tainted));
    if (!node || typeof node !== 'object') return;
    const n = node as Node;
    if (n.type === 'ExpressionTag' || n.type === 'Attribute') {
      const value = n.type === 'Attribute' ? n.value : n.expression;
      if (renders(n) && reads(value, tainted)) out.push(text(n));
      return;
    }
    const scope = eachScope(n, tainted);
    for (const [k, v] of Object.entries(n)) {
      if (k !== 'expression' || n.type !== 'EachBlock') walk(v, scope);
    }
  }
  walk(ast.fragment, new Set());
  return out;
}

describe('speaker placeholder labels are localised', () => {
  it('flags a raw speaker read, including loop variables and multi-line expressions', () => {
    expect(
      findRawSpeakerExpressions('{#each visibleSpeakers as s (s)}<li>{s}</li>{/each}')
    ).toEqual(['{s}']);
    expect(findRawSpeakerExpressions('<p>{hit.speakers\n  .join(", ")}</p>')).toHaveLength(1);
    expect(
      findRawSpeakerExpressions('{#each speakers as s}<li>{translateSpeakerLabel(s)}</li>{/each}')
    ).toEqual([]);
  });

  it('no component renders a raw API speaker label', () => {
    const root = join(__dirname, '..', '..');
    const offenders: string[] = [];
    for (const file of svelteFiles(join(root, 'components')).concat(
      svelteFiles(join(root, 'routes'))
    )) {
      for (const e of findRawSpeakerExpressions(readFileSync(file, 'utf8'))) {
        offenders.push(`${file.slice(root.length + 1)}: ${e.replace(/\s+/g, ' ')}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
