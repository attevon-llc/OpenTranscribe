import { describe, it, expect } from 'vitest';
import {
  findOccurrences,
  createFindState,
  nextIndex,
  previousIndex,
  type TextBlock,
} from './findInText';

const blocks: TextBlock[] = [
  { id: 'a', text: 'The quick brown fox' },
  { id: 'b', text: 'jumps over the lazy dog' },
  { id: 'c', text: 'THE END the the' },
];

describe('findOccurrences', () => {
  it('returns nothing for an empty query', () => {
    expect(findOccurrences(blocks, '')).toEqual([]);
    expect(findOccurrences(blocks, '   ')).toEqual([]);
  });

  it('finds case-insensitive matches in document order with positions', () => {
    const occ = findOccurrences(blocks, 'the');
    // "The"(a) + "the"(b) + "THE","the","the"(c) = 5
    expect(occ.length).toBe(5);
    expect(occ[0]).toMatchObject({ blockIndex: 0, blockId: 'a', start: 0, length: 3 });
    expect(occ[1].blockId).toBe('b');
    expect(occ[2].blockId).toBe('c');
    // last block has three "the" occurrences at distinct offsets
    const inC = occ.filter((o) => o.blockId === 'c');
    expect(inC.map((o) => o.start)).toEqual([0, 8, 12]);
  });

  it('skips blocks with non-string text', () => {
    const dirty = [
      { id: 1, text: 'ok' },
      { id: 2, text: null as unknown as string },
    ];
    expect(findOccurrences(dirty, 'ok').length).toBe(1);
  });
});

describe('createFindState', () => {
  it('sets a query and starts at the first match', () => {
    const state = createFindState();
    const snap = state.setQuery(blocks, 'fox');
    expect(snap.total).toBe(1);
    expect(snap.current).toBe(0);
    expect(snap.currentOccurrence?.blockId).toBe('a');
  });

  it('wraps forward and backward', () => {
    const state = createFindState();
    state.setQuery(blocks, 'the'); // 5 matches, current = 0
    expect(state.next().current).toBe(1);
    expect(state.previous().current).toBe(0);
    expect(state.previous().current).toBe(4); // wrap to last
    expect(state.next().current).toBe(0); // wrap to first
  });

  it('clears to an empty state', () => {
    const state = createFindState();
    state.setQuery(blocks, 'the');
    const snap = state.clear();
    expect(snap.total).toBe(0);
    expect(snap.current).toBe(-1);
    expect(snap.currentOccurrence).toBeNull();
  });

  it('handles no matches', () => {
    const state = createFindState();
    const snap = state.setQuery(blocks, 'zzz');
    expect(snap.total).toBe(0);
    expect(snap.current).toBe(-1);
    expect(state.next().current).toBe(-1);
  });
});

describe('nextIndex / previousIndex', () => {
  it('wraps around', () => {
    expect(nextIndex(2, 3)).toBe(0);
    expect(nextIndex(0, 3)).toBe(1);
    expect(previousIndex(0, 3)).toBe(2);
    expect(previousIndex(1, 3)).toBe(0);
  });

  it('returns -1 when empty', () => {
    expect(nextIndex(0, 0)).toBe(-1);
    expect(previousIndex(0, 0)).toBe(-1);
  });
});
