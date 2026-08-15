import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';
import path from 'node:path';

/**
 * Dedicated Vitest config (kept separate from vite.config.ts so the SvelteKit build
 * config stays clean). Aliases MUST mirror svelte.config.js — keep in sync, and so
 * must `define`: a component importing a build constant that only vite.config.ts
 * defines would throw ReferenceError under jsdom.
 */
export default defineConfig({
  // vite-plugin-svelte 7 dropped the `hot` plugin option (HMR is now inferred
  // from Vite's own dev/build mode; vitest never runs in dev mode, so there's
  // nothing left to opt out of).
  plugins: [svelte(), svelteTesting()],
  define: {
    __APP_VERSION__: JSON.stringify('0.0.0-test'),
    __BUILD_TIME__: JSON.stringify('1970-01-01T00:00:00.000Z'),
  },
  resolve: {
    conditions: ['browser'],
    alias: {
      $lib: path.resolve('./src/lib'),
      $components: path.resolve('./src/components'),
      $stores: path.resolve('./src/stores'),
      // `$app/*` is injected by the SvelteKit Vite plugin, which is not loaded
      // here. Without these, any component importing `goto` / `page` / `browser`
      // fails to RESOLVE, and vitest reports an unresolvable import as "no tests
      // in file" rather than a failure — so the file is silently excluded from
      // both the suite and the coverage denominator.
      //
      // ADDED 2026-08-12: `$app/stores` and `$app/environment` were missing, which
      // made 12 files untestable and invisible to coverage — SettingsModal (1711
      // LOC), login/+page (1654), Navbar (1356), search/+page (1146),
      // stores/gallery (566), accept-invite, reset-password, verify-email,
      // +error, +layout (auth bootstrap + route guard), SpeakerPreviewPlayer,
      // AppContent. That is the entire unauthenticated attack surface. Any new
      // `$app/*` import needs a stub here the same day it lands.
      '$app/navigation': path.resolve('./src/test-mocks/app-navigation.ts'),
      '$app/stores': path.resolve('./src/test-mocks/app-stores.ts'),
      '$app/environment': path.resolve('./src/test-mocks/app-environment.ts'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,js}'],
    exclude: ['node_modules/**', '.svelte-kit/**'],
    // Coverage via `npm run test:coverage`. Thresholds are RATCHET FLOORS set just below
    // the measured baseline — raise them as component test coverage grows, never lower them.
    //
    // RATCHETED 2026-08-11: the floors were still the 2026-06-04 values (1.5% lines) while
    // measured coverage had reached 11.99%. A floor 8x below actual is a gate that cannot
    // fail: coverage could have regressed by seven eighths and still reported green. Floors
    // now sit ~1.5 points under measured (lines 11.99, statements 11.79, functions 11.68,
    // branches 10.37), which absorbs normal drift without excusing a real regression.
    coverage: {
      provider: 'v8',
      reporter: ['text-summary', 'lcov'],
      // `.js` is in the glob because `src/stores/theme.js` is the app's only
      // JavaScript source file and a `{ts,svelte}` glob made the dark-mode store
      // invisible to coverage — it read as fully-untested-but-absent rather than
      // as a gap. Keep `js` here even if theme.js is later converted.
      include: ['src/**/*.{ts,js,svelte}'],
      exclude: [
        'src/**/*.{test,spec}.{ts,js}',
        'src/**/*.d.ts',
        'src/test-setup.ts',
        // Vitest-only stubs for `$app/*`. Test scaffolding in the denominator
        // inflates the percentage without covering a line of production code.
        'src/test-mocks/**',
      ],
      // RATCHETED 2026-08-12: measured lines 14.23 / statements 13.47 /
      // functions 13.81 / branches 12.76, against a denominator that GREW by
      // 2,071 lines when the `$app/stores` + `$app/environment` aliases and the
      // `.js` glob made 13 previously-unreachable files visible (lines
      // 28,488 → 30,559). A denominator change is the one time these floors must
      // be re-derived rather than nudged: the old 10.5 floor was set against a
      // smaller universe and would have gone on passing while the newly-visible
      // files sat at 0%.
      thresholds: {
        lines: 12.7,
        statements: 12,
        functions: 12.3,
        branches: 11.2,
      },
    },
  },
});
