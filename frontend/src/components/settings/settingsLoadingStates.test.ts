/**
 * Settings panels load one way (#1243 item 6): the shared `ui/SettingsSkeleton`, drawn in the
 * shape of the content. Before this, panels each carried their own grey chip/bar/line boxes that
 * resembled nothing on the page, plain "Loading..." text, or a page-sized spinner.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'fs';
import { join, resolve } from 'path';

const DIR = resolve(__dirname);
const panels = readdirSync(DIR)
  .filter((f) => f.endsWith('.svelte'))
  .map((f) => ({ name: f, source: readFileSync(join(DIR, f), 'utf8') }));

/** Files that legitimately draw their own placeholders, with the reason. */
const OWN_SKELETON_ALLOWLIST: Record<string, string> = {
  'SettingsModalShell.svelte':
    'the pre-hydration shell paints the whole dialog frame and must import nothing heavy',
};

/** Panels still on the old bare-text state, with the reason they are not converted here. */
const BARE_TEXT_ALLOWLIST: Record<string, string> = {
  'ASRSettings.svelte':
    'owned by the ASR provider/model-dropdown workstream (#1243 item 4); convert it there',
};

const OWN_SKELETON_CSS = /^\s*\.skeleton-[\w-]+[^{]*\{/m;
const BARE_LOADING_TEXT = /<div class="loading(?:-state)?">\s*\{\$t\(/;

describe('settings loading states', () => {
  it('scans a real number of panels (the guard is not vacuous)', () => {
    expect(panels.length).toBeGreaterThan(40);
  });

  it('no panel defines its own ad-hoc skeleton boxes', () => {
    const offenders = panels
      .filter((p) => !(p.name in OWN_SKELETON_ALLOWLIST))
      .filter((p) => OWN_SKELETON_CSS.test(p.source))
      .map((p) => p.name);
    expect(offenders, 'use <SettingsSkeleton> from $components/ui/SettingsSkeleton.svelte').toEqual(
      []
    );
  });

  it('no panel animates its own skeleton-pulse', () => {
    const offenders = panels
      .filter((p) => !(p.name in OWN_SKELETON_ALLOWLIST))
      .filter((p) => p.source.includes('skeleton-pulse'))
      .map((p) => p.name);
    expect(offenders).toEqual([]);
  });

  it('no panel shows bare "Loading" text in place of its content', () => {
    const offenders = panels
      .filter((p) => !(p.name in BARE_TEXT_ALLOWLIST))
      .filter((p) => BARE_LOADING_TEXT.test(p.source))
      .map((p) => p.name);
    expect(offenders).toEqual([]);
  });

  it('bare-text allowlist entries carry a reason and still match', () => {
    for (const [file, reason] of Object.entries(BARE_TEXT_ALLOWLIST)) {
      expect(reason.trim().length, file).toBeGreaterThan(10);
      expect(BARE_LOADING_TEXT.test(panels.find((p) => p.name === file)?.source ?? ''), file).toBe(
        true
      );
    }
  });

  it('the detectors fire on each shape they forbid and stay quiet otherwise', () => {
    expect(OWN_SKELETON_CSS.test('  .skeleton-chip {\n  }')).toBe(true);
    expect(BARE_LOADING_TEXT.test(`<div class="loading">{$t('x')}</div>`)).toBe(true);
    expect(OWN_SKELETON_CSS.test('  .section-title {\n  }')).toBe(false);
    expect(BARE_LOADING_TEXT.test('<div class="loading-more">text</div>')).toBe(false);
  });
});
