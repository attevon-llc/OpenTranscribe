/**
 * A menu row must never change its box on hover, press or focus.
 *
 * Every dropdown, menu and listbox scrolls itself (`overflow-y: auto`, which computes
 * overflow-x to auto too), and its rows fill the content width exactly. A `scale()` or
 * `translate*()` on a row's :hover therefore pushes it past the scrollport: a horizontal
 * scrollbar appears, shifts the row out from under the pointer, the hover ends, the scrollbar
 * goes, and the menu flickers. Measured in Chromium before this test: 2px of horizontal overflow
 * on the transcript Export and Download menus and both gallery selection menus, a 1.02 transform
 * on every row of the sort and speaker menus too.
 *
 * Two places can reintroduce it, so both are policed:
 *  1. a GLOBAL rule on the bare `button` element (that is how it happened: form-elements.css
 *     scaled every <button> on hover, and each menu inherited it without saying so);
 *  2. a component's own :hover/:active/:focus rule on a menu row.
 * Feedback goes in background, colour or outline; shift content with `padding-inline-start`.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'fs';
import { join, resolve } from 'path';

const svelteSources = import.meta.glob('/src/**/*.svelte', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

// Read from disk: under vitest a `?raw` glob of `.css` resolves to nothing, which would make the
// global-stylesheet check pass vacuously. The non-vacuity test below pins the count.
const STYLES = resolve(__dirname, '../styles');
const globalCss: Record<string, string> = Object.fromEntries(
  readdirSync(STYLES)
    .filter((f) => f.endsWith('.css'))
    .map((f) => [f, readFileSync(join(STYLES, f), 'utf8')])
);

const anchoredPanelSource = readFileSync(
  resolve(__dirname, '../lib/actions/anchoredPanel.ts'),
  'utf8'
);

interface Rule {
  selector: string;
  body: string;
}

/** Innermost `selector { body }` pairs, comments stripped, so rules nested in @media count too. */
function rules(css: string): Rule[] {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '');
  return [...clean.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selector: m[1].trim().replace(/\s+/g, ' '),
    body: m[2],
  }));
}

function styleBlocks(svelte: string): string {
  return [...svelte.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
}

const INTERACTION = /:(hover|active|focus|focus-visible|focus-within)\b/;

/** A transform that moves or resizes the box. `transform: none` is the fix, not a finding. */
function movesBox(body: string): boolean {
  return (
    /\btransform\s*:\s*[^;]*\b(translate[XYZ3d]*|scale[XYZ3d]*)\s*\(/i.test(body) ||
    /(^|;|\s)(translate|scale)\s*:\s*(?!none)\S/i.test(body)
  );
}

/**
 * Selector compounds that are the bare `button` element plus only pseudo-classes, e.g.
 * `button:hover:not(:disabled)`. `button.primary:hover` and `.btn:hover` are opt-in, not global.
 */
function isBareButtonInteraction(selector: string): boolean {
  return selector.split(',').some((part) => {
    const last =
      part
        .trim()
        .split(/\s+|>|\+|~/)
        .filter(Boolean)
        .pop() ?? '';
    return (
      /^button(:[\w-]+(\([^)]*\))?)+$/.test(last.replace(/:not\([^)]*\)/g, ':x')) &&
      INTERACTION.test(last)
    );
  });
}

/** Global stylesheet rules that transform every <button> on interaction. */
export function globalButtonTransformFindings(css: string): string[] {
  return rules(css)
    .filter((r) => isBareButtonInteraction(r.selector) && movesBox(r.body))
    .map((r) => r.selector);
}

/** What a menu row or a menu container is called in this codebase's component CSS. */
const MENU_PART =
  /(dropdown-(item|menu|content|panel|option)|menu-item|menuitem|\bmenu\b|\.option|option-item|suggestion-item|suggestions-dropdown|results-list|searchable-(list|dropdown)|listbox|popover|role=['"]?(menu|option|listbox))/i;

/** Component rules that transform a menu row (or anything inside a menu) on interaction. */
export function menuItemTransformFindings(svelte: string): string[] {
  return rules(styleBlocks(svelte))
    .filter((r) =>
      r.selector.split(',').some((part) => MENU_PART.test(part) && INTERACTION.test(part))
    )
    .filter((r) => movesBox(r.body))
    .map((r) => r.selector);
}

describe('menu hover geometry convention: the detectors can fire', () => {
  it('flags the global button scale this test exists for', () => {
    const css = `button:hover:not(:disabled) { background: red; transform: scale(1.02); }`;
    expect(globalButtonTransformFindings(css)).toEqual(['button:hover:not(:disabled)']);
    expect(
      globalButtonTransformFindings(`button:active { transform: translateY(1px) }`)
    ).toHaveLength(1);
    expect(
      globalButtonTransformFindings(`@media (hover: hover) { button:hover { scale: 1.02 } }`)
    ).toHaveLength(1);
  });

  it('leaves opt-in classes and non-moving hover rules alone', () => {
    const css = `
      .btn-primary:hover:not(:disabled) { transform: scale(1.02); }
      button.primary:hover { transform: scale(1.02); }
      button:hover:not(:disabled) { background-color: var(--button-hover); }
      button:active { transform: none; }
      .refresh-icon { transition: transform 0.3s ease; }`;
    expect(globalButtonTransformFindings(css)).toEqual([]);
  });

  it('flags a menu row that moves on hover, press or focus', () => {
    const svelte = `<div/><style>
      .dropdown-item:hover { transform: translateX(2px); }
      .export-dropdown-content button:active { transform: scale(0.98); }
      @media (min-width: 1px) { .menu-item:focus-visible { transform: translate(0, -1px); } }
    </style>`;
    expect(menuItemTransformFindings(svelte)).toEqual([
      '.dropdown-item:hover',
      '.export-dropdown-content button:active',
      '.menu-item:focus-visible',
    ]);
  });

  it('leaves the documented fix and non-menu buttons alone', () => {
    const svelte = `<div/><style>
      .dropdown-item:hover { background: var(--hover-color); padding-inline-start: calc(1rem + 2px); }
      .dropdown-item:hover { transform: none; }
      .action-btn:hover:not(:disabled) { transform: scale(1.02); }
      .dropdown-item { transition: transform 0.2s; }
    </style>`;
    expect(menuItemTransformFindings(svelte)).toEqual([]);
  });
});

describe('menu hover geometry convention: the tree', () => {
  it('has no global stylesheet that transforms every <button> on interaction', () => {
    expect(Object.keys(globalCss)).toEqual(
      expect.arrayContaining(['form-elements.css', 'theme.css'])
    );
    expect(rules(globalCss['form-elements.css']).length).toBeGreaterThan(50);
    const found = Object.entries(globalCss).flatMap(([path, css]) =>
      globalButtonTransformFindings(css).map((s) => `${path}: ${s}`)
    );
    expect(found).toEqual([]);
  });

  it('inspects the menus it is meant to police (the scan is not vacuous)', () => {
    const scanned = Object.entries(svelteSources)
      .filter(([, src]) =>
        rules(styleBlocks(src)).some(
          (r) => MENU_PART.test(r.selector) && INTERACTION.test(r.selector)
        )
      )
      .map(([p]) => p.split('/').pop());
    expect(scanned).toEqual(
      expect.arrayContaining([
        'TranscriptActionsBar.svelte',
        'GallerySelectionActions.svelte',
        'SortDropdown.svelte',
        'UserDropdown.svelte',
        'SegmentSpeakerDropdown.svelte',
        'SearchAutocomplete.svelte',
      ])
    );
  });

  it('has no component rule that moves a menu row on hover, press or focus', () => {
    const found = Object.entries(svelteSources).flatMap(([path, src]) =>
      menuItemTransformFindings(src).map((s) => `${path.split('/').pop()}: ${s}`)
    );
    expect(found).toEqual([]);
  });

  it('keeps rows inside anchored panels flat (no inherited global button shadow)', () => {
    expect(anchoredPanelSource).toMatch(/setAttribute\(\s*'data-floating-panel'/);
    const formCss = globalCss['form-elements.css'];
    const flat = rules(formCss).find(
      (r) => r.selector.includes('[data-floating-panel]') && /^\S*:where\(/.test(r.selector)
    );
    expect(flat?.body).toMatch(/box-shadow:\s*none/);
  });
});
