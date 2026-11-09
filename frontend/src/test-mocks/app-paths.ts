/**
 * Stub for SvelteKit's `$app/paths` under Vitest.
 *
 * The app has no `paths.base` (it is served from `/`), so `resolve` returns the route id with
 * its parameters substituted, as the real function does: `[name]` is replaced by the value and
 * an optional `[[name]]` segment is dropped when the value is absent.
 */

export function resolve(route: string, params: Record<string, string | undefined> = {}): string {
  const path = route
    .replace(/\/\[\[(\w+)\]\]/g, (_m, name: string) => (params[name] ? `/${params[name]}` : ''))
    .replace(/\[(\w+)\]/g, (_m, name: string) => params[name] ?? '');
  return path === '' ? '/' : path;
}
