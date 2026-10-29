/**
 * The shared display formatters live in `$lib/utils/formatting` and nowhere else. Before this
 * rule 30 components each defined their own `formatDate`/`formatBytes`/`formatDuration`, and the
 * copies had drifted: the same instant rendered as "Oct 29, 2026, 01:04 PM" in one table and
 * "10/29/2026, 1:04:00 PM" in the next, and the same byte count as "0 Bytes" or "0 B".
 *
 * Static, on purpose: it reads every component, script and store under `src/`, so a copy added
 * anywhere fails here even if no render test ever mounts it. A component that needs a different
 * shape (an i18n fallback, a unit other than seconds) names its function for what it does
 * (`describeMinutes`, `relativeFromNow`) rather than reusing a shared name.
 */
import { describe, it, expect } from 'vitest';

const sources = import.meta.glob(
  ['/src/**/*.{svelte,ts,js}', '!/src/**/*.test.ts', '!/src/**/*.spec.ts'],
  { query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

const HOME = '/src/lib/utils/formatting.ts';

const SHARED_NAME =
  '(?:formatDate|formatDateTime|formatTimestamp|formatBytes|formatFileSize|formatDuration|formatRelative\\w*)';

/** A definition: `function name`, or `const|let|var name` (arrow or function expression). */
const DEFINITION = new RegExp(`\\b(?:function\\s+|(?:const|let|var)\\s+)(${SHARED_NAME})\\b`, 'g');

const blank = (m: string) => m.replace(/[^\n]/g, ' ');

function stripComments(src: string): string {
  return src
    .replace(/<!--[\s\S]*?-->/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|[^:\\'"`\w])\/\/.*$/gm, (_m, lead: string) => lead);
}

/** `<name>@<line>` for each shared-formatter name defined in `src`. */
export function findDefinitions(src: string): string[] {
  const code = stripComments(src);
  const found: string[] = [];
  for (const m of code.matchAll(DEFINITION)) {
    const line = code.slice(0, m.index).split('\n').length;
    found.push(`${m[1]}@${line}`);
  }
  return found;
}

describe('shared formatters are defined only in lib/utils/formatting.ts', () => {
  it('scans a real tree', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(100);
    expect(sources[HOME]).toBeDefined();
  });

  it('flags a function declaration', () => {
    expect(findDefinitions('function formatDate(s) { return s; }')).toEqual(['formatDate@1']);
  });

  it('flags an arrow const and a relative-time variant', () => {
    const src = 'const formatBytes = (n) => n;\n  function formatRelativeTime(d) {}';
    expect(findDefinitions(src)).toEqual(['formatBytes@1', 'formatRelativeTime@2']);
  });

  it('flags every shared name', () => {
    for (const name of [
      'formatDate',
      'formatDateTime',
      'formatTimestamp',
      'formatBytes',
      'formatFileSize',
      'formatDuration',
      'formatRelative',
    ]) {
      expect(findDefinitions(`function ${name}(x) {}`)).toEqual([`${name}@1`]);
    }
  });

  it('stays clean for calls, imports, comments and other names', () => {
    const src = [
      "import { formatDate, formatDuration } from '$lib/utils/formatting';",
      '// function formatDate(x) {}',
      '/* const formatBytes = 1 */',
      'const label = formatDate(x);',
      'function formatClock(s) {}',
      'function describeMinutes(m) {}',
      'const relativeFromNow = () => 1;',
    ].join('\n');
    expect(findDefinitions(src)).toEqual([]);
  });

  it('finds no definition outside formatting.ts', () => {
    const offenders = Object.entries(sources)
      .filter(([path]) => path !== HOME)
      .flatMap(([path, src]) => findDefinitions(src).map((d) => `${path}::${d}`));
    expect(offenders).toEqual([]);
  });
});
