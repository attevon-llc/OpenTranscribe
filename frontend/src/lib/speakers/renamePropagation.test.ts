import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { createRenamePropagationTracker } from './renamePropagation';

describe('createRenamePropagationTracker', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('counts up on begin and down on settle, never below zero', () => {
    const tr = createRenamePropagationTracker(1000);
    tr.begin();
    tr.begin();
    expect(get(tr.pending)).toBe(2);
    tr.settle();
    tr.settle();
    tr.settle();
    expect(get(tr.pending)).toBe(0);
    tr.dispose();
  });

  it('expires a rename whose completion event never arrives', () => {
    const tr = createRenamePropagationTracker(1000);
    tr.begin();
    vi.advanceTimersByTime(1000);
    expect(get(tr.pending)).toBe(0);
  });

  it('a settled rename does not expire a second time', () => {
    const tr = createRenamePropagationTracker(1000);
    tr.begin();
    tr.begin();
    tr.settle();
    vi.advanceTimersByTime(500);
    tr.begin();
    vi.advanceTimersByTime(600); // second begin's original timer fires; third still pending
    expect(get(tr.pending)).toBe(1);
    tr.dispose();
  });
});
