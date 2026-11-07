/**
 * Stub for SvelteKit's `$app/paths` under Vitest.
 *
 * The app has no `paths.base` (it is served from `/`), so `resolve` of a route id with no
 * parameters is the route id itself, which is what the real function returns here.
 */

export function resolve(route: string): string {
  return route;
}
