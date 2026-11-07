import { describe, it, expect } from 'vitest';
import { formatEstimatedCost, parseLimit, percentUsed } from './llmUsageView';

describe('llmUsageView', () => {
  it('has no percentage without a limit and caps at 100', () => {
    expect(percentUsed(500, 0)).toBeNull();
    expect(percentUsed(250, 1000)).toBe(25);
    expect(percentUsed(5000, 1000)).toBe(100);
  });

  it('shows no cost when unpriced and keeps sub-cent costs visible', () => {
    expect(formatEstimatedCost(null, 'en')).toBeNull();
    expect(formatEstimatedCost(0, 'en')).toBe('$0.00');
    expect(formatEstimatedCost(0.0042, 'en')).toBe('$0.0042');
    expect(formatEstimatedCost(12.5, 'en')).toBe('$12.50');
  });

  it('reads a blank limit as unlimited and refuses a negative one', () => {
    expect(parseLimit('')).toBe(0);
    expect(parseLimit(null)).toBe(0);
    expect(parseLimit('1,000,000')).toBe(1_000_000);
    expect(parseLimit(2500)).toBe(2500);
    expect(parseLimit('-1')).toBeNull();
    expect(parseLimit('abc')).toBeNull();
  });
});
