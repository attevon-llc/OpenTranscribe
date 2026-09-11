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

/** Callbacks registered via {@link beforeNavigate}, newest last. */
export const beforeNavigateCallbacks: Array<(navigation: unknown) => void> = [];

/**
 * Stub for SvelteKit's navigation guard.
 *
 * Added with issue #787's unsaved-changes guard: the real `beforeNavigate` is
 * `onMount`-based and lives in the router, so a component calling it under Vitest
 * threw `beforeNavigate is not a function` and every test of that PAGE failed —
 * `routes/files/[id]/page.test.ts` went from 8 green to 8 red on an import, not on
 * any behaviour. Registrations are recorded rather than dropped so a test can drive
 * them; clear `beforeNavigateCallbacks` in a `beforeEach` when asserting on it.
 */
export function beforeNavigate(callback: (navigation: unknown) => void): void {
  beforeNavigateCallbacks.push(callback);
}
