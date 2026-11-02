import { describe, it, expect } from 'vitest';
import { opensSummaryView, seekTimeFromSearch } from './deepLinks';

describe('opensSummaryView', () => {
  it('is true only for view=summary, with or without a section', () => {
    expect(opensSummaryView('?view=summary')).toBe(true);
    expect(opensSummaryView('?view=summary&section=3')).toBe(true);
    expect(opensSummaryView('?view=transcript')).toBe(false);
    expect(opensSummaryView('?t=12')).toBe(false);
    expect(opensSummaryView('')).toBe(false);
  });
});

describe('seekTimeFromSearch', () => {
  it('reads a non-negative number of seconds, else null', () => {
    expect(seekTimeFromSearch('?t=12.5')).toBe(12.5);
    expect(seekTimeFromSearch('?t=0')).toBe(0);
    expect(seekTimeFromSearch('?t=-3')).toBeNull();
    expect(seekTimeFromSearch('?t=abc')).toBeNull();
    expect(seekTimeFromSearch('?view=summary')).toBeNull();
  });
});
