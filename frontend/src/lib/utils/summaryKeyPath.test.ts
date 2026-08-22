/**
 * Tests for `$lib/utils/summaryKeyPath` — resolves a backend `key_path` against an
 * already-loaded `summary_data` tree so `SummaryModal` knows which leaf to scroll to.
 *
 * DEFECT THIS CATCHES: the path-parsing regex must match
 * `backend/app/services/search/summary_search.py::_get_by_path` (`[^.[\]]+|\[\d+\]`)
 * exactly, or a path the backend produced silently fails to resolve on the frontend
 * and the modal opens without scrolling — no error, just the wrong (top-of-page)
 * result. These cases pin the shapes that path can actually take.
 */
import { describe, it, expect } from 'vitest';
import { resolveSummaryKeyPath } from './summaryKeyPath';

describe('resolveSummaryKeyPath', () => {
  it('resolves a bare top-level key', () => {
    expect(resolveSummaryKeyPath({ bluf: 'Ship it Friday.' }, 'bluf')).toBe('Ship it Friday.');
  });

  it('resolves a nested array index', () => {
    const data = { major_topics: [{ topic: 'Budget' }, { topic: 'Hiring' }] };
    expect(resolveSummaryKeyPath(data, 'major_topics[1].topic')).toBe('Hiring');
  });

  it('resolves a deeply nested array-of-array path', () => {
    const data = {
      major_topics: [{ topic: 'Budget', key_points: ['Cut travel', 'Freeze hiring'] }],
    };
    expect(resolveSummaryKeyPath(data, 'major_topics[0].key_points[1]')).toBe('Freeze hiring');
  });

  it('resolves a plain string array element', () => {
    expect(
      resolveSummaryKeyPath({ key_decisions: ['Approved', 'Deferred'] }, 'key_decisions[0]')
    ).toBe('Approved');
  });

  it('returns null for a missing key', () => {
    expect(resolveSummaryKeyPath({ bluf: 'x' }, 'brief_summary')).toBeNull();
  });

  it('returns null for an out-of-range array index', () => {
    expect(resolveSummaryKeyPath({ key_decisions: ['only one'] }, 'key_decisions[5]')).toBeNull();
  });

  it('returns null when the resolved leaf is not a string (e.g. a number or object)', () => {
    expect(resolveSummaryKeyPath({ count: 3 }, 'count')).toBeNull();
    expect(resolveSummaryKeyPath({ major_topics: [{ topic: 'x' }] }, 'major_topics[0]')).toBeNull();
  });

  it('returns null for an empty key_path', () => {
    expect(resolveSummaryKeyPath({ bluf: 'x' }, '')).toBeNull();
  });

  it('returns null when indexing into a non-array', () => {
    expect(resolveSummaryKeyPath({ bluf: 'x' }, 'bluf[0]')).toBeNull();
  });

  it('returns null when descending a key into a non-object', () => {
    expect(resolveSummaryKeyPath({ bluf: 'x' }, 'bluf.nested')).toBeNull();
  });
});
