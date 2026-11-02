import { describe, it, expect } from 'vitest';
import { withShareState, type Shareable } from './shareState';

const items: (Shareable & { name: string })[] = [
  { uuid: 'a', name: 'A', is_shared: false },
  { uuid: 'b', name: 'B', is_shared: false },
];

describe('withShareState', () => {
  it('sets the flag and timestamp on one entry only, without mutating the input', () => {
    const next = withShareState(items, 'b', true, '2026-01-01T00:00:00Z');
    expect(next[1]).toEqual({
      uuid: 'b',
      name: 'B',
      is_shared: true,
      shared_at: '2026-01-01T00:00:00Z',
    });
    expect(next[0]).toBe(items[0]);
    expect(items[1].is_shared).toBe(false);
  });

  it('restores the previous state (rollback is the same call)', () => {
    const shared = withShareState(items, 'a', true, 'T');
    const back = withShareState(shared, 'a', false, undefined);
    expect(back[0].is_shared).toBe(false);
    expect(back[0].shared_at).toBeUndefined();
  });

  it('leaves the list alone for an unknown uuid', () => {
    expect(withShareState(items, 'zzz', true, 'T')).toEqual(items);
  });
});
