/**
 * One modal chrome (#1244 item 4).
 *
 * Every dialog in the app is built on `ui/BaseModal.svelte`: one overlay, radius, shadow,
 * header with a close button, body padding, footer row, mobile full-screen behaviour,
 * focus trap and Escape handling. Before this, two modals (collection create/edit and the
 * TXT export options) re-declared that chrome "on purpose", six more drew their own, and
 * the same Cancel/Save row came out a different size in each.
 *
 * This fails when a component ships its OWN dimmed, full-viewport, fixed overlay, or the
 * class names that mean one (`.modal-overlay`, `.modal-backdrop`, `.modal-container`,
 * `.modal-dialog`). A genuine exception goes in ALLOWED with a written reason; an entry that
 * no longer matches anything fails, so an exemption cannot outlive its component.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';

const SRC = resolve(__dirname, '..');
const CHROME_OWNER = 'components/ui/BaseModal.svelte';

/** `file (relative to src/) -> why it may draw its own full-viewport overlay`. */
const ALLOWED: Record<string, string> = {
  'components/settings/SettingsModalFrame.svelte':
    'the settings shell is a two-pane app surface (sidebar + content), not a title/body/footer dialog; it shares --modal-backdrop and --z-modal',
  'lib/components/ClassificationBanner.svelte':
    'mandatory consent interstitial: classification-coloured header, no close button, not dismissible by Escape or backdrop',
  'components/LoginBanner.svelte':
    'mandatory login consent banner: classification-coloured header, no close button, not dismissible',
  'components/Navbar.svelte': 'scrim behind the mobile navigation drawer, not a dialog',
};

const CLASS_NAMES = /\.(modal-overlay|modal-backdrop|modal-container|modal-dialog)\b/;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (full.endsWith('.svelte')) out.push(full);
  }
  return out;
}

/** Style rules that draw a dimmed, full-viewport, fixed layer. */
function overlayRules(source: string): string[] {
  const style = /<style[^>]*>([\s\S]*)<\/style>/.exec(source)?.[1] ?? '';
  const hits: string[] = [];
  for (const rule of style.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = rule[1].trim().split('\n').pop()!.trim();
    const body = rule[2];
    if (!/position:\s*fixed/.test(body)) continue;
    const full =
      /inset:\s*0\b/.test(body) ||
      (/\btop:\s*0\b/.test(body) &&
        /\bleft:\s*0\b/.test(body) &&
        (/\bright:\s*0\b/.test(body) || /width:\s*100(%|vw)/.test(body)));
    const dim =
      /background(-color)?:\s*(rgba\(\s*0\s*,\s*0\s*,\s*0|var\(--modal-backdrop|var\(--overlay)/.test(
        body
      );
    if (full && dim) hits.push(selector);
  }
  return hits;
}

function offenders(source: string): string[] {
  const style = /<style[^>]*>([\s\S]*)<\/style>/.exec(source)?.[1] ?? '';
  const named = CLASS_NAMES.test(style) ? ['chrome class names'] : [];
  return [...overlayRules(source), ...named];
}

describe('overlayRules (the detector itself)', () => {
  it('fires on a hand-rolled modal backdrop', () => {
    const src = `<div/><style>.x-backdrop { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0, 0, 0, 0.5); }</style>`;
    expect(overlayRules(src)).toEqual(['.x-backdrop']);
  });

  it('fires on the inset shorthand and on the shared backdrop token', () => {
    const src = `<style>.a { position: fixed; inset: 0; background-color: var(--modal-backdrop); }</style>`;
    expect(overlayRules(src)).toEqual(['.a']);
  });

  it('stays quiet for a fixed toolbar, a non-dimmed layer and a plain class', () => {
    const src = `<style>
      .bar { position: fixed; top: 0; left: 0; right: 0; background: var(--surface-color); }
      .pop { position: fixed; top: 10px; left: 10px; background: rgba(0, 0, 0, 0.5); }
      .card { position: relative; inset: 0; background: rgba(0, 0, 0, 0.5); }
    </style>`;
    expect(overlayRules(src)).toEqual([]);
  });

  it('flags the chrome class names even without the fixed rule', () => {
    expect(offenders(`<style>.modal-dialog { max-width: 500px; }</style>`)).toEqual([
      'chrome class names',
    ]);
  });
});

describe('modal chrome convention', () => {
  const files = walk(SRC).map((f) => ({ rel: relative(SRC, f).replace(/\\/g, '/'), full: f }));

  it('finds the component tree it is meant to scan', () => {
    expect(files.length).toBeGreaterThan(100);
    expect(files.some((f) => f.rel === CHROME_OWNER)).toBe(true);
  });

  it('no component draws its own modal overlay chrome', () => {
    const bad = files
      .filter((f) => f.rel !== CHROME_OWNER && !(f.rel in ALLOWED))
      .map((f) => ({ file: f.rel, found: offenders(readFileSync(f.full, 'utf8')) }))
      .filter((r) => r.found.length > 0);
    expect(
      bad,
      'Build the dialog on ui/BaseModal (title, body, footer slot) instead of re-declaring overlay chrome'
    ).toEqual([]);
  });

  it('every allowlist entry still matches something', () => {
    const stale = Object.keys(ALLOWED).filter((rel) => {
      const file = files.find((f) => f.rel === rel);
      return !file || offenders(readFileSync(file.full, 'utf8')).length === 0;
    });
    expect(stale).toEqual([]);
  });

  it('BaseModal itself still owns the overlay', () => {
    const own = readFileSync(join(SRC, CHROME_OWNER), 'utf8');
    expect(overlayRules(own).length).toBeGreaterThan(0);
  });
});
