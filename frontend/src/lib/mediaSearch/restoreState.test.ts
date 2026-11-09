import { describe, expect, it } from 'vitest';
import { dateRangeFromStore, searchRestoreAction } from './restoreState';

const store = { lastSearchParams: 'q=a&p=1', resultsLength: 3, query: 'a' };

describe('searchRestoreAction', () => {
  it('does nothing without a query', () => {
    expect(searchRestoreAction('', 'q=a&p=1', store)).toBe('none');
  });

  it('reuses results whose params match exactly, to restore the scroll too', () => {
    expect(searchRestoreAction('a', 'q=a&p=1', store)).toBe('reuse-cached');
  });

  it('keeps held results for the same query under different params', () => {
    expect(searchRestoreAction('a', 'q=a&p=2', store)).toBe('keep-results');
  });

  it('searches when the store has no results, or results for another query', () => {
    expect(searchRestoreAction('a', 'q=a&p=1', { ...store, resultsLength: 0 })).toBe('search');
    expect(searchRestoreAction('b', 'q=b&p=1', store)).toBe('search');
  });

  it('does not treat empty cached params as a hit with no results', () => {
    expect(
      searchRestoreAction('a', 'q=a&p=1', {
        ...store,
        resultsLength: 0,
        lastSearchParams: 'q=a&p=1',
      })
    ).toBe('search');
  });
});

describe('dateRangeFromStore', () => {
  it('is null when neither bound is set', () => {
    expect(dateRangeFromStore(null, null)).toBeNull();
  });

  it('reads each bound as local midnight, leaving a missing bound null', () => {
    const range = dateRangeFromStore('2026-03-01', null)!;
    expect(range.from).toEqual(new Date('2026-03-01T00:00:00'));
    expect(range.to).toBeNull();
    expect(dateRangeFromStore(null, '2026-03-09')!.to).toEqual(new Date('2026-03-09T00:00:00'));
  });
});
