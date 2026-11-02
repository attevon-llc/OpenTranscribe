import { describe, it, expect, vi } from 'vitest';
import { galleryStore } from './gallery';

type Pair = {
  name: string;
  fire: () => void;
  on: (callback: (value: number) => void) => () => void;
};

const pairs: Pair[] = [
  {
    name: 'tags',
    fire: () => galleryStore.triggerTags(),
    on: (callback) => galleryStore.onTagsTrigger(callback),
  },
  {
    name: 'add tags',
    fire: () => galleryStore.triggerAddTags(),
    on: (callback) => galleryStore.onAddTagsTrigger(callback),
  },
  {
    name: 'remove tags',
    fire: () => galleryStore.triggerRemoveTags(),
    on: (callback) => galleryStore.onRemoveTagsTrigger(callback),
  },
];

describe.each(pairs)('$name trigger', ({ fire, on }) => {
  it('does not call back for the value it subscribed on', () => {
    const callback = vi.fn();
    const off = on(callback);
    expect(callback).not.toHaveBeenCalled();
    off();
  });

  it('calls back once per firing with a counter that increases', () => {
    const callback = vi.fn();
    const off = on(callback);
    fire();
    fire();
    off();
    expect(callback).toHaveBeenCalledTimes(2);
    const [first, second] = callback.mock.calls.map((call) => call[0] as number);
    expect(first).toBeGreaterThan(0);
    expect(second).toBe(first + 1);
  });

  it('stops calling back after unsubscribing', () => {
    const callback = vi.fn();
    const off = on(callback);
    fire();
    off();
    fire();
    expect(callback).toHaveBeenCalledTimes(1);
  });

  it('does not replay earlier firings to a later subscriber', () => {
    fire();
    const callback = vi.fn();
    const off = on(callback);
    expect(callback).not.toHaveBeenCalled();
    off();
  });
});

describe('the three tag triggers are independent', () => {
  it('firing one leaves the other two silent', () => {
    const calls = { tags: vi.fn(), add: vi.fn(), remove: vi.fn() };
    const offs = [
      galleryStore.onTagsTrigger(calls.tags),
      galleryStore.onAddTagsTrigger(calls.add),
      galleryStore.onRemoveTagsTrigger(calls.remove),
    ];

    galleryStore.triggerAddTags();
    expect([calls.tags, calls.add, calls.remove].map((fn) => fn.mock.calls.length)).toEqual([
      0, 1, 0,
    ]);
    galleryStore.triggerRemoveTags();
    expect([calls.tags, calls.add, calls.remove].map((fn) => fn.mock.calls.length)).toEqual([
      0, 1, 1,
    ]);
    galleryStore.triggerTags();
    expect([calls.tags, calls.add, calls.remove].map((fn) => fn.mock.calls.length)).toEqual([
      1, 1, 1,
    ]);

    offs.forEach((off) => off());
  });
});
