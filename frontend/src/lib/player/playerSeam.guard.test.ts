/**
 * The player seam holds (issue #1327).
 *
 * Plyr is being replaced. That is only safe while exactly one module talks to it: the adapter
 * `lib/player/plyrHandle.ts`, behind the library-neutral `PlayerHandle` in `types.ts`. Every
 * host (file-detail player, mini player, floating preview, search and speakers pages) asks the
 * seam instead, so swapping the library changes the adapter and its hosts' one import.
 *
 * This fails on any import of a player library outside its allowed home, in source AND tests
 * (a test that mocks `plyr` directly would silently keep testing the old library after the
 * swap): static and side-effect imports, dynamic `import()`, `vi.mock`/`vi.doMock` and CSS
 * `@import`. Plyr may appear only in the adapter and its test. The successor's packages may
 * appear only under `lib/player/` and `components/player/`, where the plan puts its adapter
 * and composed controls. An ALLOWED entry that no longer matches anything fails too, so the
 * exemption cannot outlive the adapter.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';

const SRC = resolve(__dirname, '../..');

/** `file (relative to src/) -> why it may import Plyr`. */
const PLYR_ALLOWED: Record<string, string> = {
  'lib/player/plyrHandle.ts': 'the Plyr adapter: the one module that constructs Plyr',
  'lib/player/plyrHandle.test.ts': "the adapter's own unit test, which fakes Plyr",
};

/** Directories (relative to src/) where the replacement library's packages may be imported. */
const SUCCESSOR_HOMES = ['lib/player/', 'components/player/'];

const PLYR = /^plyr(\/|$)/;
const SUCCESSOR = /^(@videojs\/|video\.js(\/|$)|media-chrome(\/|$)|vidstack(\/|$)|@vidstack\/)/;

const SPECIFIER_PATTERNS = [
  /\bfrom\s*['"]([^'"]+)['"]/g,
  /\bimport\s*['"]([^'"]+)['"]/g,
  /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
  /\bvi\.(?:mock|doMock|unmock|importActual)\s*(?:<[^>]*>)?\s*\(\s*['"]([^'"]+)['"]/g,
  /@import\s+(?:url\()?\s*['"]([^'"]+)['"]/g,
];

/** Every module specifier a source file imports, mocks or `@import`s. */
export function importedSpecifiers(source: string): string[] {
  const found = new Set<string>();
  for (const pattern of SPECIFIER_PATTERNS) {
    for (const match of source.matchAll(pattern)) found.add(match[1]);
  }
  return [...found];
}

/** The player-library specifiers `file` (relative to src/) may not import. */
export function seamViolations(file: string, source: string): string[] {
  return importedSpecifiers(source).filter((specifier) => {
    if (PLYR.test(specifier)) return !(file in PLYR_ALLOWED);
    if (SUCCESSOR.test(specifier)) return !SUCCESSOR_HOMES.some((home) => file.startsWith(home));
    return false;
  });
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|js|svelte|css)$/.test(full)) out.push(full);
  }
  return out;
}

/** This file holds the scanner's own must-fire samples, so it is the one file not scanned. */
const SELF = 'lib/player/playerSeam.guard.test.ts';

const FILES = walk(SRC)
  .map((full) => ({
    file: relative(SRC, full).split('\\').join('/'),
    source: readFileSync(full, 'utf8'),
  }))
  .filter(({ file }) => file !== SELF);

describe('the scanner (guarding the guard)', () => {
  it('finds every import form a Plyr dependency can take', () => {
    const source = [
      "import Plyr from 'plyr';",
      "import 'plyr/dist/plyr.css';",
      "const m = await import('plyr');",
      "vi.mock('plyr', () => ({}));",
      "@import 'plyr/dist/plyr.css';",
    ].join('\n');
    expect(seamViolations('components/Foo.svelte', source)).toEqual(['plyr', 'plyr/dist/plyr.css']);
    expect(importedSpecifiers("vi.mock<typeof import('x')>('plyr')")).toContain('plyr');
  });

  it('keeps the successor library inside the player folders', () => {
    const source = "import '@videojs/html/ui/play-button';";
    expect(seamViolations('routes/files/[id]/+page.svelte', source)).toEqual([
      '@videojs/html/ui/play-button',
    ]);
    expect(seamViolations('components/player/MediaPlayer.svelte', source)).toEqual([]);
    expect(seamViolations('lib/player/videojsHandle.ts', source)).toEqual([]);
  });

  it('lets only the adapter import Plyr, and ignores look-alike names', () => {
    expect(seamViolations('lib/player/plyrHandle.ts', "import Plyr from 'plyr';")).toEqual([]);
    expect(seamViolations('lib/player/types.ts', "import Plyr from 'plyr';")).toEqual(['plyr']);
    expect(
      seamViolations('components/Foo.svelte', "import X from '$components/PlyrMiniPlayer.svelte';")
    ).toEqual([]);
    expect(seamViolations('components/Foo.svelte', "import X from 'plyrish';")).toEqual([]);
  });

  it('reads the real tree (the walk is not empty)', () => {
    expect(FILES.length).toBeGreaterThan(500);
    expect(FILES.some(({ file }) => file === 'components/VideoPlayer.svelte')).toBe(true);
    expect(FILES.some(({ file }) => file === SELF)).toBe(false);
  });
});

describe('the player seam', () => {
  it('no module outside its allowed home imports a player library', () => {
    const offenders = FILES.flatMap(({ file, source }) =>
      seamViolations(file, source).map((specifier) => `${file} -> ${specifier}`)
    );
    expect(offenders).toEqual([]);
  });

  it('every Plyr exemption still names a file that imports Plyr', () => {
    const importers = new Set(
      FILES.filter(({ source }) => importedSpecifiers(source).some((s) => PLYR.test(s))).map(
        ({ file }) => file
      )
    );
    const stale = Object.keys(PLYR_ALLOWED).filter((file) => !importers.has(file));
    expect(stale).toEqual([]);
  });
});
