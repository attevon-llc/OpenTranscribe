import { describe, it, expect } from 'vitest';
import { opensSummaryView } from './deepLinks';

describe('opensSummaryView', () => {
  it('is true only for view=summary, with or without a section', () => {
    expect(opensSummaryView('?view=summary')).toBe(true);
    expect(opensSummaryView('?view=summary&section=3')).toBe(true);
    expect(opensSummaryView('?view=transcript')).toBe(false);
    expect(opensSummaryView('?t=12')).toBe(false);
    expect(opensSummaryView('')).toBe(false);
  });
});
