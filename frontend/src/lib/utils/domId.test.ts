import { describe, it, expect } from 'vitest';
import { domId } from './domId';

describe('domId', () => {
  it('never hands out the same id twice, whatever the prefix', () => {
    const ids = [domId('a'), domId('a'), domId('b')];
    expect(new Set(ids).size).toBe(3);
    expect(ids[0]).toMatch(/^a-\d+$/);
    expect(ids[2]).toMatch(/^b-\d+$/);
  });
});
