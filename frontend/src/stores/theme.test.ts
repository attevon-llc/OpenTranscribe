/**
 * Tests for `$stores/theme` — the dark-mode store.
 *
 * DEFECT THESE CATCH: `theme.js` is the app's only `.js` source file, and
 * `vitest.config.ts` had `coverage.include: ['src/**\/*.{ts,svelte}']`. The store
 * that decides light vs dark for the whole UI was therefore invisible to
 * coverage AND untested — it read as absent rather than as a gap. The glob is now
 * `{ts,js,svelte}`.
 *
 * The behaviour under test is not cosmetic: the store must set BOTH
 * `documentElement[data-theme]` (which every CSS custom property keys off) and
 * `body.theme-*` (which the transition-suppression rules key off). Setting only
 * one produces a half-themed page — dark colours with light-mode transitions, or
 * vice versa — and neither `npm run check` nor a screenshot of one mode catches it.
 *
 * Each test re-imports the module: the theme is applied at MODULE EVALUATION
 * time (to avoid a flash of the wrong theme), so a shared instance would only
 * ever exercise the first scenario.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

/** Make `matchMedia('(prefers-color-scheme: dark)')` report `prefersDark`. */
function stubPrefersColorScheme(prefersDark: boolean): void {
  window.matchMedia = ((query: string) => ({
    matches: prefersDark && query.includes('prefers-color-scheme: dark'),
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

async function loadThemeStore() {
  vi.resetModules();
  return import('$stores/theme');
}

const originalMatchMedia = window.matchMedia;

describe('theme store', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
    document.body.className = '';
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('honours a saved theme over the system preference', async () => {
    localStorage.setItem('theme', 'dark');
    // System says light; the explicit user choice must win.
    stubPrefersColorScheme(false);

    const { theme } = await loadThemeStore();

    expect(get(theme)).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(document.body.classList.contains('theme-dark')).toBe(true);
  });

  it('falls back to prefers-color-scheme: dark when nothing is saved', async () => {
    stubPrefersColorScheme(true);

    const { theme } = await loadThemeStore();

    expect(get(theme)).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(document.body.classList.contains('theme-dark')).toBe(true);
  });

  it('defaults to light when nothing is saved and the system prefers light', async () => {
    stubPrefersColorScheme(false);

    const { theme } = await loadThemeStore();

    expect(get(theme)).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(document.body.classList.contains('theme-light')).toBe(true);
  });

  it('sets BOTH data-theme and body.theme-* on every change, and swaps rather than stacks', async () => {
    localStorage.setItem('theme', 'light');
    stubPrefersColorScheme(false);

    const { theme } = await loadThemeStore();
    expect(document.body.classList.contains('theme-light')).toBe(true);

    theme.set('dark');

    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(document.body.classList.contains('theme-dark')).toBe(true);
    // The stale class must be REMOVED — both present at once is the half-themed
    // page this store exists to prevent.
    expect(document.body.classList.contains('theme-light')).toBe(false);
  });

  it('persists the choice so it survives a reload', async () => {
    stubPrefersColorScheme(false);

    const { theme } = await loadThemeStore();
    theme.set('dark');

    expect(localStorage.getItem('theme')).toBe('dark');
  });

  it('toggleTheme flips between the two modes', async () => {
    localStorage.setItem('theme', 'light');
    stubPrefersColorScheme(false);

    const { theme, toggleTheme } = await loadThemeStore();

    toggleTheme();
    expect(get(theme)).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    toggleTheme();
    expect(get(theme)).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('is not cleared by a session teardown — theme is a preserved preference', async () => {
    localStorage.setItem('theme', 'dark');
    stubPrefersColorScheme(false);
    await loadThemeStore();

    const { clearUserState } = await import('$lib/session/clearUserState');
    await clearUserState();

    expect(localStorage.getItem('theme')).toBe('dark');
  });
});
