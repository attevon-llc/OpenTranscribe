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
  plugins: [svelte({ hot: false }), svelteTesting()],
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
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,js}'],
    exclude: ['node_modules/**', '.svelte-kit/**'],
    // Coverage via `npm run test:coverage`. Thresholds are RATCHET FLOORS set
    // just below the measured baseline (~2% lines, 2026-06-04) — raise them as
    // component test coverage grows, never lower them.
    coverage: {
      provider: 'v8',
      reporter: ['text-summary', 'lcov'],
      include: ['src/**/*.{ts,svelte}'],
      exclude: ['src/**/*.{test,spec}.ts', 'src/**/*.d.ts', 'src/test-setup.ts'],
      thresholds: {
        lines: 1.5,
        statements: 1.5,
        functions: 1,
        branches: 2,
      },
    },
  },
});
