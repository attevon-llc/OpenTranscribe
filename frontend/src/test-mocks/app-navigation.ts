/**
 * Stub for SvelteKit's `$app/navigation` under Vitest.
 *
 * `$app/*` modules are injected by the SvelteKit Vite plugin, which
 * `vitest.config.ts` deliberately does not load — it runs components straight
 * through Vite + jsdom. A component importing `goto` therefore fails to
 * *resolve* rather than failing an assertion, which reads as a broken test file
 * instead of a missing stub. Aliased in `vitest.config.ts`.
 *
 * `goto` records its calls so a test can assert navigation without a router.
 * Clear `gotoCalls` in a `beforeEach` when asserting on it.
 */
export const gotoCalls: string[] = [];

export function goto(url: string): Promise<void> {
  gotoCalls.push(url);
  return Promise.resolve();
}

export function invalidate(): Promise<void> {
  return Promise.resolve();
}

export function invalidateAll(): Promise<void> {
  return Promise.resolve();
}
