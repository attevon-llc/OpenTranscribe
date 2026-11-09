import { describe, expect, it } from 'vitest';
import { selectionAfterClick, toggled } from './selection';

const IDS = ['a', 'b', 'c', 'd', 'e'];

describe('toggled', () => {
  it('adds an absent id and removes a present one, never mutating its input', () => {
    const original = new Set(['a']);
    expect([...toggled(original, 'b')].sort()).toEqual(['a', 'b']);
    expect([...toggled(original, 'a')]).toEqual([]);
    expect([...original]).toEqual(['a']);
  });
});

describe('selectionAfterClick', () => {
  it('selects the inclusive range between anchor and target, in either direction', () => {
    expect([...selectionAfterClick(new Set(), IDS, 'b', 'd', true)].sort()).toEqual([
      'b',
      'c',
      'd',
    ]);
    expect([...selectionAfterClick(new Set(), IDS, 'd', 'b', true)].sort()).toEqual([
      'b',
      'c',
      'd',
    ]);
  });

  it('keeps what was already selected outside the range', () => {
    expect([...selectionAfterClick(new Set(['a']), IDS, 'c', 'd', true)].sort()).toEqual([
      'a',
      'c',
      'd',
    ]);
  });

  it('toggles one file for a plain click or a shift-click with no anchor', () => {
    expect([...selectionAfterClick(new Set(['a']), IDS, 'a', 'b', false)].sort()).toEqual([
      'a',
      'b',
    ]);
    expect([...selectionAfterClick(new Set(['b']), IDS, null, 'b', true)]).toEqual([]);
  });

  it('toggles one file when the anchor or target is not in the list', () => {
    expect([...selectionAfterClick(new Set(), IDS, 'gone', 'b', true)]).toEqual(['b']);
    expect([...selectionAfterClick(new Set(), IDS, 'a', 'gone', true)]).toEqual(['gone']);
  });
});
