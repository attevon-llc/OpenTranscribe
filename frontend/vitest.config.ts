import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';
import path from 'node:path';

/**
 * Dedicated Vitest config (kept separate from vite.config.ts so the SvelteKit
 * build/PWA config stays clean). Aliases MUST mirror svelte.config.js — keep in sync.
 */
export default defineConfig({
  plugins: [svelte({ hot: false }), svelteTesting()],
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
    exclude: ['src/tests/**', 'node_modules/**', '.svelte-kit/**'],
    // Report-only coverage (no thresholds yet) — run with `npm run test:coverage`.
    coverage: {
      provider: 'v8',
      reporter: ['text-summary', 'lcov'],
      include: ['src/**/*.{ts,svelte}'],
      exclude: ['src/tests/**', 'src/**/*.{test,spec}.ts', 'src/**/*.d.ts', 'src/test-setup.ts'],
    },
  },
});
