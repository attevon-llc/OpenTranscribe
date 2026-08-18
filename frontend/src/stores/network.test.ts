/**
 * `networkStore` wires `window`'s native online/offline events into a Svelte
 * store. The riskiest behaviors are the real side effects: `initialize()` must
 * register exactly one listener per event, a second call must be a no-op
 * guarded by the module-level `initialized` flag (never doubling the handler
 * count), and the returned cleanup function must both remove the listeners AND
 * reset that guard so a later `initialize()` can re-register. Each test loads a
 * fresh module instance (mirroring `theme.test.ts`) because `initialized` and
 * the store singleton both live at module scope — reusing one instance across
 * tests would let an earlier test's registration state leak into the next.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

function setNavigatorOnline(value: boolean): void {
  Object.defineProperty(window.navigator, 'onLine', {
    value,
    writable: true,
    configurable: true,
  });
}

async function loadNetworkStore() {
  vi.resetModules();
  return import('./network');
}

const originalOnLine = window.navigator.onLine;

describe('network store', () => {
  afterEach(() => {
    setNavigatorOnline(originalOnLine);
  });

  it('seeds initial state from navigator.onLine at module load', async () => {
    setNavigatorOnline(false);
    const { networkStore } = await loadNetworkStore();

    expect(get(networkStore).online).toBe(false);
  });

  it('initialize() registers exactly one online and one offline listener on window', async () => {
    setNavigatorOnline(true);
    const { networkStore } = await loadNetworkStore();
    const addSpy = vi.spyOn(window, 'addEventListener');

    networkStore.initialize();

    expect(addSpy.mock.calls.filter(([type]) => type === 'online')).toHaveLength(1);
    expect(addSpy.mock.calls.filter(([type]) => type === 'offline')).toHaveLength(1);
    addSpy.mockRestore();
  });

  it('a second initialize() call does not double-register listeners (initialized guard)', async () => {
    setNavigatorOnline(true);
    const { networkStore } = await loadNetworkStore();
    const addSpy = vi.spyOn(window, 'addEventListener');

    const firstCleanup = networkStore.initialize();
    const callsAfterFirst = addSpy.mock.calls.length;
    const secondCleanup = networkStore.initialize();

    expect(addSpy.mock.calls.length).toBe(callsAfterFirst);
    // The guarded early return produces no cleanup function on the second call.
    expect(firstCleanup).toBeTypeOf('function');
    expect(secondCleanup).toBeUndefined();
    addSpy.mockRestore();
  });

  it('isOnline reflects a "offline" event dispatched on window after initialize()', async () => {
    setNavigatorOnline(true);
    const { networkStore, isOnline } = await loadNetworkStore();
    networkStore.initialize();
    expect(get(isOnline)).toBe(true);

    window.dispatchEvent(new Event('offline'));

    expect(get(isOnline)).toBe(false);
    expect(get(networkStore).online).toBe(false);
  });

  it('isOnline reflects an "online" event dispatched on window after initialize()', async () => {
    setNavigatorOnline(false);
    const { networkStore, isOnline } = await loadNetworkStore();
    networkStore.initialize();
    expect(get(isOnline)).toBe(false);

    window.dispatchEvent(new Event('online'));

    expect(get(isOnline)).toBe(true);
  });

  it('an online/offline event fired BEFORE initialize() has no effect — nothing is wired yet', async () => {
    setNavigatorOnline(true);
    const { networkStore, isOnline } = await loadNetworkStore();

    window.dispatchEvent(new Event('offline'));

    expect(get(isOnline)).toBe(true);
    networkStore.initialize();
  });

  it('the returned cleanup function removes both listeners and resets the initialized guard', async () => {
    setNavigatorOnline(true);
    const { networkStore } = await loadNetworkStore();
    const addSpy = vi.spyOn(window, 'addEventListener');
    const removeSpy = vi.spyOn(window, 'removeEventListener');

    const cleanup = networkStore.initialize();
    expect(cleanup).toBeTypeOf('function');
    cleanup?.();

    expect(removeSpy.mock.calls.filter(([type]) => type === 'online')).toHaveLength(1);
    expect(removeSpy.mock.calls.filter(([type]) => type === 'offline')).toHaveLength(1);

    // Guard was reset by cleanup, so a subsequent initialize() re-registers
    // rather than silently no-op'ing.
    const callsBeforeReinit = addSpy.mock.calls.length;
    const secondCleanup = networkStore.initialize();
    expect(addSpy.mock.calls.length).toBeGreaterThan(callsBeforeReinit);
    expect(secondCleanup).toBeTypeOf('function');

    addSpy.mockRestore();
    removeSpy.mockRestore();
  });

  it('refresh() re-reads navigator.onLine on demand, without waiting for an event', async () => {
    setNavigatorOnline(true);
    const { networkStore } = await loadNetworkStore();
    expect(get(networkStore).online).toBe(true);

    setNavigatorOnline(false);
    networkStore.refresh();

    expect(get(networkStore).online).toBe(false);
  });
});
