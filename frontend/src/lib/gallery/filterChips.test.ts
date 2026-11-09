import { describe, it, expect } from 'vitest';
import {
  DURATION_CHIPS,
  chipRange,
  isDurationChipActive,
  rangeFromValues,
  toggleItem,
  visibleDurationChips,
} from './filterChips';

const bounds = { min: 30, max: 900 };

describe('toggleItem', () => {
  it('adds an absent item and removes a present one without mutating', () => {
    const list = ['a'];
    expect(toggleItem(list, 'b')).toEqual(['a', 'b']);
    expect(toggleItem(['a', 'b'], 'a')).toEqual(['b']);
    expect(list).toEqual(['a']);
  });
});

describe('duration chips', () => {
  it('clamps a chip into the real bounds, with null max meaning the upper bound', () => {
    expect(chipRange(DURATION_CHIPS[0], bounds)).toEqual([60, 600]);
    expect(chipRange(DURATION_CHIPS[3], bounds)).toEqual([3600, 900]);
  });

  it('hides chips that cannot match anything in this library (#744)', () => {
    expect(visibleDurationChips(bounds).map((c) => c.code)).toEqual([
      'duration1to10',
      'duration11to30',
    ]);
  });

  it('reports null at a bound, matching the slider convention', () => {
    expect(rangeFromValues(30, 900, bounds)).toEqual({ min: null, max: null });
    expect(rangeFromValues(100, 500, bounds)).toEqual({ min: 100, max: 500 });
  });

  it('marks a chip active only when the range equals its clamped range', () => {
    const chip = DURATION_CHIPS[0];
    expect(isDurationChipActive(chip, bounds, { min: 60, max: 600 })).toBe(true);
    expect(isDurationChipActive(chip, bounds, { min: 60, max: null })).toBe(false);
  });
});
