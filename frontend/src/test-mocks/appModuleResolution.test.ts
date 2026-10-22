/**
 * Guards the `$app/*` vitest aliases.
 *
 * DEFECT THIS CATCHES: an unresolvable import is not a test failure under
 * vitest — the file fails to *transform*, vitest reports "no tests" and moves
 * on, and coverage silently drops the file from its denominator. `$app/stores`
 * and `$app/environment` were unaliased for the life of the suite, which made
 * 12 files untestable and invisible: the four unauthenticated routes
 * (login / reset-password / verify-email / accept-invite), the auth-bootstrap
 * `+layout.svelte` route guard, SettingsModal, Navbar, search, `+error`,
 * AppContent, SpeakerPreviewPlayer and `$stores/gallery`.
 *
 * Each module below is imported for its side-effect-free module graph only. If
 * someone adds a new `$app/*` import without a matching stub in
 * `vitest.config.ts`, this file goes red instead of a dozen files going quiet.
 */

import { describe, it, expect } from 'vitest';

/** Every source file that imports `$app/stores` or `$app/environment`. */
const APP_MODULE_CONSUMERS: Array<[string, () => Promise<unknown>]> = [
  ['routes/login/+page.svelte', () => import('../routes/login/+page.svelte')],
  ['routes/reset-password/+page.svelte', () => import('../routes/reset-password/+page.svelte')],
  ['routes/verify-email/+page.svelte', () => import('../routes/verify-email/+page.svelte')],
  ['routes/accept-invite/+page.svelte', () => import('../routes/accept-invite/+page.svelte')],
  ['routes/search/+page.svelte', () => import('../routes/search/+page.svelte')],
  ['routes/+error.svelte', () => import('../routes/+error.svelte')],
  ['routes/+layout.svelte', () => import('../routes/+layout.svelte')],
  ['components/Navbar.svelte', () => import('$components/Navbar.svelte')],
  ['components/SettingsModal.svelte', () => import('$components/SettingsModal.svelte')],
  ['components/AppContent.svelte', () => import('$components/AppContent.svelte')],
  [
    'components/speakers/SpeakerPreviewPlayer.svelte',
    () => import('$components/speakers/SpeakerPreviewPlayer.svelte'),
  ],
  ['stores/gallery.ts', () => import('$stores/gallery')],
];

/**
 * The route pages transitively import most of the app graph, so on a cold transform
 * cache each import is a whole-app Vite transform. Measured 2026-10-22 (48-core shared
 * host, cold cache): slowest import (search/+page) 8.0-9.6 s at load ~50 and >30 s at
 * load ~120 with the old default of 47 workers; 4.3 s cold and well under 1 s warm with
 * the vitest.config.ts worker cap + fsModuleCache. Not a hang.
 */
const ROUTE_GRAPH_IMPORT_TIMEOUT_MS = 30_000;

describe('$app/* alias coverage', () => {
  it.each(APP_MODULE_CONSUMERS)(
    '%s resolves and transforms',
    async (_name, load) => {
      const mod = (await load()) as Record<string, unknown>;
      // A Svelte component compiles to a function; a plain store module to an object.
      // Either proves the module graph resolved — the point is that it did not throw.
      expect(['function', 'object']).toContain(typeof (mod.default ?? mod));
    },
    ROUTE_GRAPH_IMPORT_TIMEOUT_MS
  );

  it('$app/stores page store exposes a URL, so route-guard code can be driven', async () => {
    const { page, setPage, resetAppStores } = await import('./app-stores');
    setPage('/login');
    let seen: URL | null = null;
    const unsub = page.subscribe((p) => (seen = p.url));
    unsub();
    expect(seen!.pathname).toBe('/login');
    resetAppStores();
  });

  it('$app/environment reports browser=true so SSR branches are never the tested path', async () => {
    const { browser, building } = await import('./app-environment');
    expect(browser).toBe(true);
    expect(building).toBe(false);
  });
});
