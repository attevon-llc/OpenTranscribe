/**
 * Stub for SvelteKit's `$app/stores` under Vitest.
 *
 * Companion to `app-navigation.ts`. `$app/*` modules are injected by the
 * SvelteKit Vite plugin, which `vitest.config.ts` deliberately does not load.
 * Without this alias, importing `page` fails to *resolve*, so the whole test
 * file errors out during transform — and vitest reports that as "no tests in
 * file" rather than a failure. Twelve files were unreachable this way,
 * including every unauthenticated route (`login`, `reset-password`,
 * `verify-email`, `accept-invite`) and the auth-bootstrap `+layout.svelte`.
 *
 * `page` is a real writable store so a test can drive route-dependent
 * behaviour (`setPage('/login')`) instead of mocking the module per file.
 * Call `resetAppStores()` in a `beforeEach` — module state is shared across
 * tests in the same file.
 */

import { writable, readable, type Writable } from 'svelte/store';

export interface MockPageState {
  url: URL;
  params: Record<string, string>;
  route: { id: string | null };
  status: number;
  error: Error | null;
  data: Record<string, unknown>;
  form: unknown;
}

const DEFAULT_URL = 'http://localhost/';

function pageStateFor(url: string): MockPageState {
  return {
    url: new URL(url, DEFAULT_URL),
    params: {},
    route: { id: null },
    status: 200,
    error: null,
    data: {},
    form: null,
  };
}

export const page: Writable<MockPageState> = writable(pageStateFor(DEFAULT_URL));

/** Point `$page.url` at `pathOrUrl` (accepts a path or an absolute URL). */
export function setPage(pathOrUrl: string, extra: Partial<MockPageState> = {}): void {
  page.set({ ...pageStateFor(pathOrUrl), ...extra });
}

/** Restore the default route state. Use in `beforeEach`. */
export function resetAppStores(): void {
  page.set(pageStateFor(DEFAULT_URL));
}

export const navigating = readable(null);
export const updated = Object.assign(readable(false), {
  check: () => Promise.resolve(false),
});

export function getStores() {
  return { page, navigating, updated };
}
