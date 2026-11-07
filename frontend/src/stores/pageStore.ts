import { toStore, type Readable } from 'svelte/store';
import { browser } from '$app/env';
import { page } from '$app/state';

export interface PageSnapshot {
  url: typeof page.url;
  status: typeof page.status;
  error: typeof page.error;
}

const snapshot = (): PageSnapshot => ({
  url: page.url,
  status: page.status,
  error: page.error,
});

/**
 * Store view of SvelteKit's rune-based `page` (`$app/state`) for legacy-mode components.
 *
 * `page` is an immutable import, so a `$:` statement reading it is flagged by
 * `svelte/no-immutable-reactive-statements`; a store dependency is the documented
 * bridge (Svelte `toStore`). The getter returns a fresh object so subscribers are
 * notified whenever `url`, `status` or `error` change, which `page` itself never signals.
 *
 * On the server `toStore` calls its getter once, when this module is first imported, and
 * `page` may only be read while a component renders (`state_read_outside_render`): every
 * server-rendered request answered 500. So the server store reads `page` when a component
 * subscribes, which happens during its render, for that request.
 */
export const pageStore: Readable<PageSnapshot> = browser
  ? toStore(snapshot)
  : {
      subscribe(run) {
        run(snapshot());
        return () => {};
      },
    };
