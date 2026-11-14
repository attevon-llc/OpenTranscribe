/**
 * Layers pinned under the navbar stack under it.
 *
 * The gallery's mobile filter drawer starts at `top: var(--content-top)` but sat on
 * `--z-modal` (1300), above the navbar (`--z-navbar`, 1200). The navbar is its own stacking
 * context and holds the open mobile menu, so the drawer painted over the menu and its scrim.
 * The search page's identical drawer used `--z-dropdown` and was fine: two hand-picked tiers
 * for one pattern. jsdom computes no stacking, so this evaluates every such rule's z-index
 * against the token scale in theme.css.
 *
 * Docs: MDN "Stacking context" (a positioned element with a z-index creates one; its children
 * cannot paint above a sibling context with a higher z-index).
 */
import fs from 'fs';
import { describe, it, expect } from 'vitest';
import { SRC, rel, walkSource, styleOf, cssRules } from './sourceTree';

const scale = new Map(
  [...fs.readFileSync(`${SRC}/styles/theme.css`, 'utf8').matchAll(/(--z-[a-z]+):\s*(\d+);/g)].map(
    (m) => [m[1] ?? '', Number(m[2])]
  )
);

/** A z-index token's value; throws on an unknown name so a renamed token cannot pass silently. */
function tier(name: string): number {
  const value = scale.get(name);
  if (value === undefined) throw new Error(`no ${name} in theme.css`);
  return value;
}

/** `var(--z-x)` or `calc(var(--z-x) +/- N)` against the token scale; a bare number as itself. */
function zValue(expr: string): number {
  const m = /^(?:calc\()?var\((--z-[a-z]+)\)(?:\s*([+-])\s*(\d+))?\)?$/.exec(expr.trim());
  if (!m) return Number(expr);
  const offset = m[3] ? Number(m[3]) * (m[2] === '-' ? -1 : 1) : 0;
  return tier(m[1] ?? '') + offset;
}

const UNDER_NAVBAR = /(?:^|;)\s*(?:top|inset):\s*var\(--content-top\)/;

/** `file selector -> z` for every rule that pins a layer to the top of the content area. */
function underNavbarLayers(files: Record<string, string>): Map<string, number> {
  const out = new Map<string, number>();
  for (const [name, source] of Object.entries(files)) {
    for (const rule of cssRules(styleOf(source))) {
      const z = /z-index:\s*([^;]+)/.exec(rule.body);
      if (z && UNDER_NAVBAR.test(rule.body))
        out.set(`${name} ${rule.selector}`, zValue(z[1] ?? ''));
    }
  }
  return out;
}

const tree = Object.fromEntries(
  walkSource(SRC, /\.(svelte|css)$/).map((f) => [rel(f), fs.readFileSync(f, 'utf8')])
);
const layers = underNavbarLayers(tree);
const navbar = tier('--z-navbar');

describe('layers pinned under the navbar', () => {
  it('reads the z-index scale', () => {
    expect(navbar).toBeGreaterThan(tier('--z-dropdown'));
  });

  it('finds the gallery and search filter drawers (control)', () => {
    const names = [...layers.keys()].join('\n');
    expect(names).toMatch(/GalleryFilterPanel\.svelte \.filter-rail/);
    expect(names).toMatch(/routes\/search\/\+page\.svelte \.filter-rail/);
  });

  it('stack below the navbar that holds the mobile menu', () => {
    expect([...layers].filter(([, z]) => !(z < navbar))).toEqual([]);
  });

  it('stack below the mobile menu scrim', () => {
    const navbarCss = styleOf(tree['components/Navbar.svelte'] ?? '');
    const scrim = cssRules(navbarCss).find(
      (r) => r.selector === '.mobile-overlay' && /z-index/.test(r.body)
    );
    const scrimZ = zValue(/z-index:\s*([^;]+)/.exec(scrim?.body ?? '')?.[1] ?? 'NaN');
    expect(scrimZ).toBeGreaterThan(0);
    expect([...layers].filter(([, z]) => !(z < scrimZ))).toEqual([]);
  });

  it('flags a drawer on the modal tier (must fire)', () => {
    const found = underNavbarLayers({
      'x.svelte':
        '<style>.d { position: fixed; top: var(--content-top); z-index: var(--z-modal); }</style>',
    });
    expect(found.get('x.svelte .d')).toBe(tier('--z-modal'));
    expect(tier('--z-modal')).toBeGreaterThanOrEqual(navbar);
  });
});
