import { describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

// The server side of `$app/state`: `page` throws unless a component is rendering, the same
// rule SvelteKit enforces (`state_read_outside_render`).
const rendering = vi.hoisted(() => ({ active: false }));

vi.mock('$app/env', () => ({ browser: false }));
vi.mock('$app/state', () => ({
  page: {
    get url() {
      if (!rendering.active) throw new Error('state_read_outside_render');
      return new URL('http://localhost/files/abc');
    },
    get status() {
      if (!rendering.active) throw new Error('state_read_outside_render');
      return 200;
    },
    get error() {
      if (!rendering.active) throw new Error('state_read_outside_render');
      return null;
    },
  },
}));

describe('pageStore on the server', () => {
  it('can be imported outside a render', async () => {
    rendering.active = false;
    await expect(import('./pageStore')).resolves.toHaveProperty('pageStore');
  });

  it('reads the current page when a rendering component subscribes', async () => {
    const { pageStore } = await import('./pageStore');
    rendering.active = true;
    try {
      expect(get(pageStore).url.pathname).toBe('/files/abc');
      expect(get(pageStore).status).toBe(200);
    } finally {
      rendering.active = false;
    }
  });
});
