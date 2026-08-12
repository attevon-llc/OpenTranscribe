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
      // here. Without this, any component importing `goto` fails to resolve and
      // its whole test file errors out before a single assertion runs.
      '$app/navigation': path.resolve('./src/test-mocks/app-navigation.ts'),
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
      include: ['src/**/*.{ts,svelte}'],
      exclude: ['src/**/*.{test,spec}.ts', 'src/**/*.d.ts', 'src/test-setup.ts'],
      thresholds: {
        lines: 10.5,
        statements: 10.5,
        functions: 10,
        branches: 9,
      },
    },
  },
});
