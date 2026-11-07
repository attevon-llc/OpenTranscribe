import { toStore } from 'svelte/store';
import { page } from '$app/state';

/**
 * Store view of SvelteKit's rune-based `page` (`$app/state`) for legacy-mode components.
 *
 * `page` is an immutable import, so a `$:` statement reading it is flagged by
 * `svelte/no-immutable-reactive-statements`; a store dependency is the documented
 * bridge (Svelte `toStore`). The getter returns a fresh object so subscribers are
 * notified whenever `url`, `status` or `error` change, which `page` itself never signals.
 */
export const pageStore = toStore(() => ({
  url: page.url,
  status: page.status,
  error: page.error,
}));
