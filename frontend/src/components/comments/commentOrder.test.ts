import { describe, it, expect } from 'vitest';
import { sortComments } from './commentOrder';

const c = (id: string, timestamp: number | null, created_at: string) => ({
  id,
  timestamp,
  created_at,
});

describe('sortComments', () => {
  it('puts general comments first, oldest first, then timed comments in playback order', () => {
    const sorted = sortComments([
      c('t-late', 90, '2026-01-01T00:00:01Z'),
      c('g-new', null, '2026-01-01T00:00:05Z'),
      c('t-zero', 0, '2026-01-01T00:00:09Z'),
      c('g-old', null, '2026-01-01T00:00:02Z'),
      c('t-early', 15, '2026-01-01T00:00:03Z'),
    ]);
    expect(sorted.map((x) => x.id)).toEqual(['g-old', 'g-new', 't-zero', 't-early', 't-late']);
  });

  it('breaks a tie between two comments at the same time by creation order', () => {
    const sorted = sortComments([
      c('second', 30, '2026-01-01T00:00:02Z'),
      c('first', 30, '2026-01-01T00:00:01Z'),
    ]);
    expect(sorted.map((x) => x.id)).toEqual(['first', 'second']);
  });

  it('does not reorder the array it was given', () => {
    const input = [c('b', 2, 'x'), c('a', 1, 'x')];
    sortComments(input);
    expect(input.map((x) => x.id)).toEqual(['b', 'a']);
  });
});
