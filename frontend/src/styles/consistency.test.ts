/**
 * Cross-page consistency guards (issue #1243, follow-up to items 12 and 16).
 *
 * The Search/Chat design audit fixed the core type scale and listed defects it deliberately left
 * on other pages. Each is a pure style-source defect that jsdom cannot observe (it computes no
 * contrast, loads no fonts and applies no cascade), so each is pinned by scanning the source:
 *
 * 1. Text coloured with `--primary-color`. That token is the BACKGROUND of every primary button
 *    and carries white text; as a text colour it is 2.9:1 on the dark card. Text and icons on a
 *    surface use `--primary-on-surface`.
 * 2. A button variant (`btn-primary` ...) without the base `.btn`. The variants carry colour
 *    only, so without `.btn` the button has no padding -- unless the component defines its own.
 * 3. A stray indigo accent (#4f46e5) next to the app's blue.
 * 4. Panel section headings in shouting-caps micro-labels, where chat uses sentence-case muted.
 *
 * Every allowlist entry carries a written reason and is itself checked for staleness.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const SRC = path.resolve(__dirname, '..');

function walk(dir: string, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(svelte|css|ts)$/.test(e.name) && !/\.test\.ts$/.test(e.name)) out.push(p);
  }
  return out;
}

const rel = (p: string) => path.relative(SRC, p).split(path.sep).join('/');
const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, '');
const STYLED = walk(SRC).filter((p) => /\.(svelte|css)$/.test(p));
const read = (p: string) => strip(fs.readFileSync(p, 'utf8'));

/** Files matching `re`, minus the allowlist; also reports allowlist entries that match nothing. */
function scan(re: RegExp, files: string[], allow: Record<string, string>) {
  const hits = new Map<string, number>();
  for (const f of files) {
    const n = (read(f).match(re) ?? []).length;
    if (n) hits.set(rel(f), n);
  }
  return {
    offenders: [...hits].filter(([f]) => !(f in allow)).map(([f, n]) => `${f} (${n})`),
    stale: Object.keys(allow).filter((f) => !hits.has(f)),
  };
}

describe('text is never coloured with the button blue', () => {
  const TEXT_PRIMARY = /(?<![-\w])color:\s*var\(--primary-color(?![-\w])/g;
  const ALLOW: Record<string, string> = {
    'components/UploadManager.svelte':
      'blue text on a solid #fff pill inside the primary-coloured header: --primary-on-surface goes pale in dark mode and would vanish on the white pill (5.17:1 as written)',
  };

  it('uses --primary-on-surface for text and icons on a surface', () => {
    expect(scan(TEXT_PRIMARY, STYLED, ALLOW).offenders).toEqual([]);
  });

  it('has no stale allowlist entries', () => {
    expect(scan(TEXT_PRIMARY, STYLED, ALLOW).stale).toEqual([]);
  });
});

describe('no stray indigo accent', () => {
  const INDIGO = /#4f46e5|rgba\(\s*79\s*,\s*70\s*,\s*229/gi;
  const ALLOW: Record<string, string> = {
    'styles/theme.css':
      '--link-visited is deliberately a different hue from the link blue so visited links read as visited',
  };
  const ALL = walk(SRC);

  it('uses the primary blue token (or its rgb channels) instead', () => {
    expect(scan(INDIGO, ALL, ALLOW).offenders).toEqual([]);
  });

  it('has no stale allowlist entries', () => {
    expect(scan(INDIGO, ALL, ALLOW).stale).toEqual([]);
  });
});

describe('button variants carry the base .btn class', () => {
  const VARIANT = /^btn-(primary|secondary|danger|warning)$/;

  /** True when the component's own <style> gives `.cls` padding, which makes `.btn` unnecessary. */
  function definesPadding(css: string, cls: string): boolean {
    for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selectors = m[1].split(',').map((s) => s.trim());
      if (selectors.includes(`.${cls}`) && /(^|[;\s])padding\s*:/.test(m[2])) return true;
    }
    return false;
  }

  function offenders(): string[] {
    const bad: string[] = [];
    for (const f of STYLED.filter((p) => p.endsWith('.svelte'))) {
      const src = read(f);
      const css = src.slice(Math.max(src.indexOf('<style'), 0));
      for (const m of src.matchAll(/class="([^"]*)"/g)) {
        const toks = m[1]
          .replace(/\{[^}]*\}/g, ' ')
          .split(/\s+/)
          .filter(Boolean);
        if (toks.includes('btn')) continue;
        if (toks.some((x) => VARIANT.test(x)) && !toks.some((x) => definesPadding(css, x))) {
          bad.push(`${rel(f)}: class="${m[1].trim()}"`);
        }
      }
    }
    return bad;
  }

  it('never uses a variant without .btn unless the component pads it itself', () => {
    expect(offenders()).toEqual([]);
  });
});

describe('panel section headings are sentence-case muted labels', () => {
  /** Selectors converted from uppercase micro-labels. Chips, badges and table headers stay uppercase. */
  const HEADINGS: [string, string][] = [
    ['components/FilterSidebar.svelte', '.filter-section h3'],
    ['components/SettingsModal.svelte', '.section-heading'],
    ['components/speakers/ClustersTab.svelte', '.section-heading-btn'],
    ['components/SpeakerStats.svelte', '.card-label'],
    ['components/SpeakerStats.svelte', '.stat-label'],
    ['components/CollectionsPanel.svelte', '.current-label'],
    ['components/gallery/BulkTagModal.svelte', '.current-label'],
    ['components/collections/CollectionsList.svelte', '.section-label'],
    ['components/collections/SharedCollectionsSection.svelte', '.section-label'],
    ['components/groups/GroupsOverview.svelte', '.groups-section-title'],
    ['components/settings/SystemStatisticsPanel.svelte', '.stat-section h4'],
    ['components/settings/LDAPSettings.svelte', '.section h3'],
    ['components/settings/OIDCSettings.svelte', '.section h3'],
    ['components/settings/SAMLSettings.svelte', '.section h3'],
    ['components/settings/PKISettings.svelte', '.section h3'],
    ['components/settings/ProxySettings.svelte', '.section h3'],
    ['components/settings/SessionSettings.svelte', '.section h3'],
    ['components/settings/LocalAuthSettings.svelte', '.section h3'],
  ];
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  it.each(HEADINGS)('%s %s has no text-transform: uppercase', (file, selector) => {
    const css = read(path.join(SRC, file));
    const rule = css.match(new RegExp(`(?:^|\\n)[ \\t]*${esc(selector)}[ \\t]*\\{([^{}]*)\\}`));
    expect(rule, `rule ${selector} not found in ${file}`).not.toBeNull();
    expect(rule![1]).not.toMatch(/text-transform:\s*uppercase/);
    expect(rule![1]).not.toMatch(/letter-spacing/);
  });
});
