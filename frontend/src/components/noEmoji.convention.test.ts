/**
 * The app draws no emoji and no emoji-like text glyph (check marks, crosses, warning signs,
 * hourglasses, gears, padlocks, bullets-as-status-dots, arrows) as an icon. Icons are SVG:
 * `ui/MenuIcon` with a named shape from `$lib/ui/menuIcons`. The owner's report that started
 * this: the upload tray flashed a colour hourglass emoji for its queued state.
 *
 * Static, on purpose: a render test only sees the states someone remembered to mount, while
 * this reads every component, script and stylesheet under `src/` and every locale string (copy
 * renders too), so a glyph added anywhere fails here even if no test ever shows it.
 *
 * What it ignores, and why that is not a loophole:
 *  - comments (`//`, `/* *\/`, `<!-- -->`): never rendered;
 *  - test files: fixtures, never rendered;
 *  - plain typographic punctuation (em dash, ellipsis, middle dot, curly and angle quotes,
 *    multiplication sign, bullet, (c) (R) TM): text, not icons.
 *
 * `ALLOWLIST` is ONLY for text that never reaches a user (a console/log string, a data constant
 * that is never displayed), keyed `<path>::<character>`, each with a written reason. A `.svelte`
 * file or a locale string can never be allowlisted: after comments are stripped, everything left
 * in them renders.
 */
import { describe, it, expect } from 'vitest';

const sources = import.meta.glob(
  ['/src/**/*.{svelte,ts,js,css,html}', '!/src/**/*.test.ts', '!/src/**/*.spec.ts'],
  { query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

const locales = import.meta.glob('/src/**/locales/*.json', {
  import: 'default',
  eager: true,
}) as Record<string, Record<string, unknown>>;

/**
 * Pictographic code points: anything Unicode classes as Extended_Pictographic (emoji, including
 * text-presentation ones such as the warning sign and the gear) except (c) (R) TM, plus the blocks
 * that hold the emoji-LIKE glyphs this app used as icons (arrows, misc technical, geometric
 * shapes such as the black circle and the triangles, dingbats such as the check mark and ballot
 * X, circled operators), and the joiners that build emoji: variation selectors (U+FE0E/F),
 * the zero-width joiner, the keycap combiner and the regional indicators of flag pairs.
 */
const PICTOGRAPHIC =
  /(?:(?![©®™])\p{Extended_Pictographic}|[←-⇿⊕-⊚⌀-⏿■-➿⤀-⥿⬀-⯿]|︎|️|‍|⃣|[\u{1F1E6}-\u{1F1FF}])/gu;

/** Blank a match but keep its newlines, so reported line numbers still point at the source. */
const blank = (m: string) => m.replace(/[^\n]/g, ' ');

export function stripComments(src: string): string {
  return src
    .replace(/<!--[\s\S]*?-->/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|[^:\\'"`\w])\/\/.*$/gm, (_m, lead: string) => lead);
}

const namedEntity = (entity: string): string => {
  const el = document.createElement('textarea');
  el.innerHTML = entity;
  return el.value;
};

/** Spell out every escape a glyph can hide behind, so `&#9888;` is caught like the glyph is. */
export function decodeEscapes(src: string): string {
  return src
    .replace(/&#x([0-9a-f]+);/gi, (_m, h: string) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_m, d: string) => String.fromCodePoint(Number(d)))
    .replace(/&[a-z][a-z0-9]*;/gi, namedEntity)
    .replace(/\\u\{([0-9a-f]+)\}/gi, (_m, h: string) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/\\u([0-9a-f]{4})/gi, (_m, h: string) => String.fromCodePoint(parseInt(h, 16)))
    .replace(
      /(content\s*:\s*)(['"])((?:\\[0-9a-f]{1,6}\s?|[^'"])*)\2/gi,
      (_m, pre, q, body: string) =>
        `${pre}${q}${body.replace(/\\([0-9a-f]{1,6})\s?/gi, (_e, h: string) =>
          String.fromCodePoint(parseInt(h, 16))
        )}${q}`
    );
}

export interface Finding {
  line: number;
  chars: string;
}

/** Pictographs in rendered text: comments stripped, escapes decoded. */
export function findPictographs(source: string, { isCode = true } = {}): Finding[] {
  const text = decodeEscapes(isCode ? stripComments(source) : source);
  const out: Finding[] = [];
  text.split('\n').forEach((line, i) => {
    const hits = line.match(PICTOGRAPHIC);
    if (hits) out.push({ line: i + 1, chars: [...new Set(hits)].join('') });
  });
  return out;
}

/** `<path>::<character>` -> reason. Only for text no user ever sees; see the header. */
const ALLOWLIST: Record<string, string> = {};

/**
 * Files another lane is editing in a parallel worktree, so this change cannot touch them. Every
 * entry is asserted to STILL contain the glyph: when that lane's fix lands this test fails until
 * the entry is deleted, so an exemption cannot outlive its reason.
 */
const PENDING_IN_ANOTHER_LANE: Record<string, string> = {
  '/src/components/FirstRunWizard.svelte':
    'The wizard progress mark draws a check mark glyph; FirstRunWizard.svelte is being rewritten ' +
    'in a parallel worktree, which must replace it with <MenuIcon name="check" />.',
};

const short = (path: string) => path.replace(/^\/src\//, '');

function codeFindings(): string[] {
  const found: string[] = [];
  for (const [path, src] of Object.entries(sources)) {
    if (path in PENDING_IN_ANOTHER_LANE) continue;
    for (const f of findPictographs(src)) {
      const unexplained = [...f.chars].filter((c) => !(`${path}::${c}` in ALLOWLIST));
      if (unexplained.length) found.push(`${short(path)}:${f.line} ${unexplained.join('')}`);
    }
  }
  return found;
}

describe('no emoji or emoji-like glyph is rendered', () => {
  it('scans the whole tree (the scan is not vacuous)', () => {
    const paths = Object.keys(sources);
    expect(paths.filter((p) => p.endsWith('.svelte')).length).toBeGreaterThan(300);
    expect(paths).toEqual(
      expect.arrayContaining([
        '/src/components/UploadProgress.svelte',
        '/src/components/Toast.svelte',
        '/src/lib/ui/menuIcons.ts',
        '/src/app.html',
      ])
    );
    expect(paths.some((p) => /\.test\.ts$/.test(p))).toBe(false);
    const core = Object.keys(locales).filter((p) => p.startsWith('/src/lib/i18n/locales/'));
    expect(core).toHaveLength(12);
  });

  it('finds none in components, scripts, stylesheets or app.html', () => {
    expect(codeFindings()).toEqual([]);
  });

  it('finds none in any locale string', () => {
    const found: string[] = [];
    for (const [path, strings] of Object.entries(locales)) {
      for (const [key, value] of Object.entries(strings)) {
        if (typeof value !== 'string') continue;
        const hits = findPictographs(value, { isCode: false });
        if (hits.length) found.push(`${short(path)} ${key}: ${hits.map((h) => h.chars).join('')}`);
      }
    }
    expect(found).toEqual([]);
  });

  it('allowlists only unrendered text, each entry with a reason, and none stale', () => {
    for (const [key, reason] of Object.entries(ALLOWLIST)) {
      const [path, char] = key.split('::');
      expect(path, key).not.toMatch(/\.svelte$|\/locales\/|\.html$/);
      expect(reason.trim().length, key).toBeGreaterThanOrEqual(20);
      const hits = findPictographs(sources[path] ?? '');
      expect(
        hits.some((h) => h.chars.includes(char)),
        `stale allowlist entry ${key}`
      ).toBe(true);
    }
  });

  it('keeps each other-lane exemption only while that file still needs it', () => {
    for (const [path, reason] of Object.entries(PENDING_IN_ANOTHER_LANE)) {
      expect(reason.trim().length, path).toBeGreaterThanOrEqual(20);
      expect(sources[path], `${path} is not scanned`).toBeTypeOf('string');
      expect(
        findPictographs(sources[path]).length,
        `${path} no longer draws a glyph: delete its PENDING_IN_ANOTHER_LANE entry`
      ).toBeGreaterThan(0);
    }
  });
});

describe('the detector', () => {
  const fires = (src: string, opts?: { isCode?: boolean }) => findPictographs(src, opts).length > 0;

  it.each([
    ['a check mark', `<span>✓</span>`],
    ['a ballot X', `{ok ? '✓' : '✗'}`],
    ['the hourglass emoji', `default: return '⏳';`],
    ['a warning sign with a variation selector', `icon="⚠️"`],
    ['a bare variation selector', `x️y`],
    ['a ZWJ sequence', `<p>\u{1F469}‍\u{1F4BB}</p>`],
    ['a flag pair', `'\u{1F1FA}\u{1F1F8}'`],
    ['a keycap', `'1️⃣'`],
    ['a padlock emoji', `badge: '\u{1F512}'`],
    ['a black circle used as a dot', `<span class="dot">●</span>`],
    ['a triangle chevron', `{open ? '▾' : '▸'}`],
    ['an arrow', `<div class="bullet">→</div>`],
    ['a decimal entity', `<span>&#9888;</span>`],
    ['a hex entity', `<span>&#x2713;</span>`],
    ['a named entity', `<span>&check;</span>`],
    ['an arrow entity', `<span>&rarr;</span>`],
    ['a JS escape', `const ok = '\\u2713';`],
    ['a JS code point escape', `const lock = '\\u{1F512}';`],
    ['a CSS content escape', `.done::before { content: '\\2713'; }`],
  ])('fires on %s', (_name, src) => {
    expect(fires(src)).toBe(true);
  });

  it('fires on locale copy, where nothing is a comment', () => {
    expect(fires('✓ Pasted from clipboard', { isCode: false })).toBe(true);
    expect(fires('Settings → Search', { isCode: false })).toBe(true);
  });

  it.each([
    ['typographic punctuation', `<p>One — two… a · b, 4×3, • “q” «yes» ‹x›</p>`],
    ['copyright and trademark signs', `<footer>© 2026 OpenTranscribe® Name™</footer>`],
    ['a line comment', `const x = 1; // ✓ done`],
    ['a block comment', `/* ⏳ queued */ const y = 2;`],
    ['an HTML comment', `<!-- ⚠️ careful --><div />`],
    ['a URL followed by a comment', `const u = 'https://example.com'; // → next`],
    ['Arabic, CJK and accented text', `<p>مرحبا 你好 été</p>`],
    ['an SVG icon', `<MenuIcon name="check" />`],
  ])('stays clean on %s', (_name, src) => {
    expect(findPictographs(src)).toEqual([]);
  });

  it('reports the source line, so a finding can be located', () => {
    expect(findPictographs(`a\n// ✓\nb '✗'`)).toEqual([{ line: 3, chars: '✗' }]);
  });
});
