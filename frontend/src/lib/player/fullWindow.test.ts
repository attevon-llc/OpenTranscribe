/**
 * The mini player's full-window fallback (plan #1327 row 28): when to use it, and its keys.
 * The component test (`components/player/MiniPlayer.controls.test.ts`) proves the wiring.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { attachFullWindowKeys, supportsElementFullscreen } from './fullWindow';

describe('supportsElementFullscreen', () => {
  it('is true with the standard or the WebKit-prefixed API, false without either', () => {
    expect(supportsElementFullscreen({ fullscreenEnabled: true })).toBe(true);
    expect(
      supportsElementFullscreen({
        fullscreenEnabled: undefined as never,
        webkitFullscreenEnabled: true,
      })
    ).toBe(true);
    expect(supportsElementFullscreen({ fullscreenEnabled: false })).toBe(false);
    // iPhone Safari: no element API at all, so both are undefined.
    expect(supportsElementFullscreen({ fullscreenEnabled: undefined as never })).toBe(false);
  });
});

describe('attachFullWindowKeys', () => {
  let cleanup: (() => void) | null = null;
  afterEach(() => {
    cleanup?.();
    cleanup = null;
    document.body.innerHTML = '';
  });

  const setup = () => {
    document.body.innerHTML =
      '<div id="c"><button id="b"></button><input id="i" /></div><p id="out"></p>';
    let on = false;
    const container = document.getElementById('c')!;
    cleanup = attachFullWindowKeys(container, { isOn: () => on, set: (v) => (on = v) });
    const key = (id: string, init: KeyboardEventInit) =>
      document
        .getElementById(id)!
        .dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }));
    return { key, state: () => on };
  };

  it('toggles on F inside the player and leaves on Escape from anywhere', () => {
    const { key, state } = setup();
    key('b', { key: 'f' });
    expect(state()).toBe(true);
    key('out', { key: 'Escape' });
    expect(state()).toBe(false);
    key('b', { key: 'F' });
    key('b', { key: 'f' });
    expect(state()).toBe(false);
  });

  it('ignores F outside the player, while typing, with a modifier and on key repeat', () => {
    const { key, state } = setup();
    key('out', { key: 'f' });
    key('i', { key: 'f' });
    key('b', { key: 'f', ctrlKey: true });
    key('b', { key: 'f', repeat: true });
    expect(state()).toBe(false);
  });

  it('stops listening after cleanup', () => {
    const { key, state } = setup();
    cleanup?.();
    cleanup = null;
    key('b', { key: 'f' });
    expect(state()).toBe(false);
  });
});
