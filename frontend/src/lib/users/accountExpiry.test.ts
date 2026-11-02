import { describe, it, expect } from 'vitest';
import { toDateInputValue, toEndOfDayTimestamp } from './accountExpiry';

describe('toDateInputValue', () => {
  it('keeps the date part of a stored timestamp', () => {
    expect(toDateInputValue('2026-03-15T23:59:59Z')).toBe('2026-03-15');
    expect(toDateInputValue('2026-03-15T23:59:59+02:00')).toBe('2026-03-15');
  });

  it('is empty when no expiry is set', () => {
    expect(toDateInputValue(null)).toBe('');
    expect(toDateInputValue(undefined)).toBe('');
    expect(toDateInputValue('')).toBe('');
  });
});

describe('toEndOfDayTimestamp', () => {
  it('sends the last second of the chosen day', () => {
    expect(toEndOfDayTimestamp('2026-06-01')).toBe('2026-06-01T23:59:59');
  });

  it('sends null for an emptied field, which clears the expiry', () => {
    expect(toEndOfDayTimestamp('')).toBeNull();
  });
});
