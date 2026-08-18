/**
 * `settingsModalStore` tracks which settings section is open and per-section
 * "unsaved changes" dirty flags. These tests focus on the two operations that
 * are easy to get subtly wrong: `clearAllDirty` rebuilds the dirty map via
 * `Object.keys().reduce()` (a dropped/mistyped key here would silently remove
 * a section from every future dirty check), and `hasAnyDirty` is a plain
 * function over a passed-in state snapshot rather than a derived store, so it
 * must be exercised as `hasAnyDirty(get(settingsModalStore))`, not subscribed to.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { settingsModalStore } from './settingsModalStore';

beforeEach(() => {
  settingsModalStore.reset();
});

describe('open / close', () => {
  it('open() sets isOpen and defaults to the system-statistics section', () => {
    settingsModalStore.open();

    const state = get(settingsModalStore);
    expect(state.isOpen).toBe(true);
    expect(state.activeSection).toBe('system-statistics');
  });

  it('open(section) opens directly on the given section', () => {
    settingsModalStore.open('profile');

    expect(get(settingsModalStore).activeSection).toBe('profile');
  });

  it('close() resets isOpen and the active section, but leaves dirty state alone', () => {
    settingsModalStore.open('profile');
    settingsModalStore.setDirty('profile', true);

    settingsModalStore.close();

    const state = get(settingsModalStore);
    expect(state.isOpen).toBe(false);
    expect(state.activeSection).toBe('system-statistics');
    expect(state.dirtyState.profile).toBe(true);
  });
});

describe('setActiveSection', () => {
  it('changes only activeSection, leaving isOpen and dirtyState untouched', () => {
    settingsModalStore.open('profile');
    settingsModalStore.setDirty('profile', true);

    settingsModalStore.setActiveSection('recording');

    const state = get(settingsModalStore);
    expect(state.activeSection).toBe('recording');
    expect(state.isOpen).toBe(true);
    expect(state.dirtyState.profile).toBe(true);
  });
});

describe('setDirty / clearDirty', () => {
  it('setDirty flips only the named section', () => {
    settingsModalStore.setDirty('profile', true);

    const state = get(settingsModalStore);
    expect(state.dirtyState.profile).toBe(true);
    expect(state.dirtyState.recording).toBe(false);
  });

  it('clearDirty clears only the named section', () => {
    settingsModalStore.setDirty('profile', true);
    settingsModalStore.setDirty('recording', true);

    settingsModalStore.clearDirty('profile');

    const state = get(settingsModalStore);
    expect(state.dirtyState.profile).toBe(false);
    expect(state.dirtyState.recording).toBe(true);
  });
});

describe('clearAllDirty', () => {
  it('rebuilds the dirty map so every known section is false, not just the ones that were dirty', () => {
    settingsModalStore.setDirty('profile', true);
    settingsModalStore.setDirty('chat', true);
    settingsModalStore.setDirty('billing', true);

    settingsModalStore.clearAllDirty();

    const state = get(settingsModalStore);
    expect(Object.values(state.dirtyState).every((isDirty) => isDirty === false)).toBe(true);
  });

  it('preserves every original section key — the reduce must not drop or add keys', () => {
    const originalKeys = Object.keys(get(settingsModalStore).dirtyState).sort();

    settingsModalStore.setDirty('profile', true);
    settingsModalStore.clearAllDirty();

    const rebuiltKeys = Object.keys(get(settingsModalStore).dirtyState).sort();
    expect(rebuiltKeys).toEqual(originalKeys);
  });
});

describe('hasAnyDirty', () => {
  it('is a plain function over a passed-in state snapshot, not a derived store', () => {
    const cleanState = get(settingsModalStore);
    expect(settingsModalStore.hasAnyDirty(cleanState)).toBe(false);

    settingsModalStore.setDirty('profile', true);
    const dirtyState = get(settingsModalStore);
    expect(settingsModalStore.hasAnyDirty(dirtyState)).toBe(true);

    // The snapshot taken BEFORE the mutation must not reflect it — proving
    // this reads the passed argument, not live store state.
    expect(settingsModalStore.hasAnyDirty(cleanState)).toBe(false);
  });

  it('returns false once every section has been cleared again', () => {
    settingsModalStore.setDirty('profile', true);
    settingsModalStore.setDirty('chat', true);
    settingsModalStore.clearAllDirty();

    expect(settingsModalStore.hasAnyDirty(get(settingsModalStore))).toBe(false);
  });
});

describe('reset', () => {
  it('restores the full initial state, including dirty flags', () => {
    settingsModalStore.open('profile');
    settingsModalStore.setDirty('profile', true);

    settingsModalStore.reset();

    const state = get(settingsModalStore);
    expect(state.isOpen).toBe(false);
    expect(state.activeSection).toBe('system-statistics');
    expect(state.dirtyState.profile).toBe(false);
  });
});
