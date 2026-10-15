/**
 * Floating panels flip and clamp (owner feedback, #1245 theme).
 *
 * A dropdown, menu, popover or suggestion list that is `position: absolute` under its
 * trigger opens downward whatever the room, runs off the bottom of the viewport and is
 * clipped by any `overflow: auto` ancestor (the scrolling filter sidebar, a modal body). The
 * fix is one shared action, `$lib/actions/anchoredPanel`, which portals the panel to
 * `<body>`, flips it above when there is no room below, caps its height and clamps it
 * horizontally (RTL included).
 *
 * This fails when a `.svelte` file styles a dropdown/menu/popover/listbox/suggestion panel
 * `position: absolute|fixed` without using the action. A genuine exception goes in ALLOWED
 * with a written reason; an entry that no longer matches anything fails, so an exemption
 * cannot outlive its component. A native `<select>` is exempt by nature (the browser flips it)
 * and tooltips are not panels (they are hover-only, non-interactive, pointer-events: none).
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';

const SRC = resolve(__dirname, '..');

/** `file (relative to src/) -> why it may position its own panel`. */
const ALLOWED: Record<string, string> = {
  'components/ui/DateRangePicker.svelte':
    'positions its popover with computePopoverPlacement directly (the geometry anchoredPanel is built on) because it also needs the bottom-sheet mode under 560px',
  'components/NotificationsPanel.svelte':
    'a viewport-pinned panel under the fixed top bar (top/right offsets, max-height to the viewport, scrolls itself); it is not anchored to a trigger that can scroll',
  'components/Navbar.svelte':
    'the recording-controls popup hangs off a control inside the fixed top bar, which never scrolls and always has the whole page below it',
  'components/chat/ChatControlsPanel.svelte':
    'a dialog pinned to the chat header with its own viewport-bounded max-height and internal scroll; the header is at the top of the page so there is always room below',
  'components/chat/ChatTracePanel.svelte':
    'a docked full-height side drawer (top/right/bottom: 0), not a panel anchored to a trigger',
  'components/VideoPlayer.svelte':
    'styles the third-party Plyr control menus via :global(); Plyr positions and sizes them',
  'components/transcript/SpeakerEditorPanel.svelte':
    'two hover-only informational popups (legend, match preview) revealed by CSS :hover, not interactive menus',
  'components/settings/LLMConfigModal.svelte':
    'info-tooltip pseudo-element (::after) revealed on hover, not a panel',
  'components/settings/PromptSettings.svelte':
    'info-tooltip pseudo-element (::after) revealed on hover, not a panel',
  'routes/register/+page.svelte':
    'password-requirements tooltip revealed on hover/focus of an info icon, not an interactive panel',
};

const PANEL_NAME =
  /(dropdown|menu|popover|listbox|suggestion|autocomplete|picker|popup|tooltip|panel|(?<![a-z])layer|flyout)/i;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (full.endsWith('.svelte')) out.push(full);
  }
  return out;
}

/** Selectors of rules that are `position: absolute|fixed` and name a panel (or sit on a menu/listbox role). */
export function floatingPanelRules(source: string): string[] {
  const style = (/<style[^>]*>([\s\S]*)<\/style>/.exec(source)?.[1] ?? '').replace(
    /\/\*[\s\S]*?\*\//g,
    ''
  );
  const markup = source.replace(/<style[^>]*>[\s\S]*<\/style>/, '');
  const roleClasses = new Set<string>();
  for (const tag of markup.matchAll(/<[a-z][^>]*role="(?:menu|listbox)"[^>]*>/g)) {
    for (const cls of /class="([^"]*)"/.exec(tag[0])?.[1].split(/\s+/) ?? []) {
      if (cls) roleClasses.add(cls);
    }
  }
  const hits: string[] = [];
  for (const rule of style.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = rule[1].trim().split('\n').pop()!.trim();
    if (!/position:\s*(absolute|fixed)/.test(rule[2])) continue;
    const named = PANEL_NAME.test(selector);
    const onRole = [...roleClasses].some((c) => selector.includes(`.${c}`));
    if (named || onRole) hits.push(selector);
  }
  return hits;
}

export function usesAnchoredPanel(source: string): boolean {
  return /use:anchoredPanel\b/.test(source) || /\banchoredPanel\(/.test(source);
}

function offenders(source: string): string[] {
  return usesAnchoredPanel(source) ? [] : floatingPanelRules(source);
}

describe('floatingPanelRules (the detector itself)', () => {
  it('fires on a hand-rolled absolute dropdown panel', () => {
    const src = `<div class="x-dropdown"/><style>.x-dropdown { position: absolute; top: 100%; }</style>`;
    expect(offenders(src)).toEqual(['.x-dropdown']);
  });

  it('fires on a fixed menu, and on a role="listbox" element with an unrelated class', () => {
    expect(offenders(`<style>.row-menu { position: fixed; }</style>`)).toEqual(['.row-menu']);
    const src = `<ul class="results" role="listbox"></ul><style>.results { position: absolute; }</style>`;
    expect(offenders(src)).toEqual(['.results']);
  });

  it('stays clean when the file uses the action', () => {
    const src = `<div class="x-dropdown" use:anchoredPanel={{ anchor }}/><style>.x-dropdown { overflow-y: auto; }</style>`;
    expect(offenders(src)).toEqual([]);
  });

  it('stays clean for an absolutely positioned element that is not a panel', () => {
    const src = `<div class="badge"/><style>.badge { position: absolute; top: 0; }</style>`;
    expect(offenders(src)).toEqual([]);
  });

  it('stays clean for a panel that is not absolutely positioned', () => {
    const src = `<div class="x-dropdown"/><style>.x-dropdown { position: relative; }</style>`;
    expect(offenders(src)).toEqual([]);
  });
});

describe('floating panels use the anchoredPanel action', () => {
  const files = walk(SRC).map((f) => ({
    rel: relative(SRC, f).split('\\').join('/'),
    source: readFileSync(f, 'utf8'),
  }));

  it('no component positions its own dropdown/menu/popover panel', () => {
    const bad = files
      .filter((f) => !(f.rel in ALLOWED))
      .map((f) => ({ file: f.rel, rules: offenders(f.source) }))
      .filter((f) => f.rules.length > 0);
    expect(bad).toEqual([]);
  });

  it('every ALLOWED entry still matches a file that positions a panel without the action', () => {
    const stale = Object.keys(ALLOWED).filter((rel) => {
      const f = files.find((x) => x.rel === rel);
      return !f || offenders(f.source).length === 0;
    });
    expect(stale).toEqual([]);
  });
});
