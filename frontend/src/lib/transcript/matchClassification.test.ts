import { describe, it, expect } from 'vitest';
import {
  buildTimeRanges,
  overlapsAny,
  classifySegments,
  countByType,
  highlightClassifiedText,
} from './matchClassification';

describe('buildTimeRanges', () => {
  it('splits keyword vs semantic occurrences by has_keyword_match', () => {
    const { keywordRanges, semanticRanges } = buildTimeRanges([
      { start_time: 1, end_time: 2, has_keyword_match: true },
      { start_time: 5, end_time: 6, has_keyword_match: false },
    ]);
    expect(keywordRanges).toEqual([{ start: 1, end: 2 }]);
    expect(semanticRanges).toEqual([{ start: 5, end: 6 }]);
  });

  it('is empty for no occurrences', () => {
    expect(buildTimeRanges([])).toEqual({ keywordRanges: [], semanticRanges: [] });
  });
});

describe('overlapsAny', () => {
  it('detects a genuine overlap', () => {
    expect(overlapsAny(1, 3, [{ start: 2, end: 4 }])).toBe(true);
  });

  it('rejects adjacent (touching, not overlapping) ranges', () => {
    expect(overlapsAny(1, 2, [{ start: 2, end: 4 }])).toBe(false);
  });

  it('rejects ranges before or after every candidate', () => {
    expect(overlapsAny(10, 11, [{ start: 2, end: 4 }])).toBe(false);
  });
});

describe('classifySegments', () => {
  const occurrences = [
    { start_time: 0, end_time: 1, has_keyword_match: true },
    { start_time: 5, end_time: 6, has_keyword_match: false },
  ];

  it('classifies a segment overlapping a keyword occurrence', () => {
    const result = classifySegments([{ uuid: 'a', start_time: 0, end_time: 1 }], occurrences);
    expect(result).toEqual({ a: 'keyword' });
  });

  it('classifies a segment overlapping only a semantic occurrence', () => {
    const result = classifySegments([{ uuid: 'b', start_time: 5, end_time: 6 }], occurrences);
    expect(result).toEqual({ b: 'semantic' });
  });

  it('keyword wins when a segment overlaps both a keyword and a semantic range', () => {
    const result = classifySegments([{ uuid: 'c', start_time: 0, end_time: 6 }], occurrences);
    expect(result).toEqual({ c: 'keyword' });
  });

  it('omits a segment that overlaps neither range', () => {
    const result = classifySegments([{ uuid: 'd', start_time: 100, end_time: 101 }], occurrences);
    expect(result).toEqual({});
  });

  it('skips a segment with no uuid rather than keying the result on "undefined"', () => {
    const result = classifySegments(
      [
        { uuid: undefined, start_time: 0, end_time: 1 } as unknown as {
          uuid: string;
          start_time: number;
          end_time: number;
        },
      ],
      occurrences
    );
    expect(result).toEqual({});
  });
});

describe('countByType', () => {
  it('counts keyword and semantic occurrences separately', () => {
    expect(
      countByType([
        { start_time: 0, end_time: 1, has_keyword_match: true },
        { start_time: 1, end_time: 2, has_keyword_match: true },
        { start_time: 2, end_time: 3, has_keyword_match: false },
      ])
    ).toEqual({ keyword: 2, semantic: 1 });
  });

  it('is zero/zero for an empty list', () => {
    expect(countByType([])).toEqual({ keyword: 0, semantic: 0 });
  });
});

describe('highlightClassifiedText', () => {
  it('wraps a semantic segment whole', () => {
    expect(highlightClassifiedText('hello world', 'semantic', 'anything')).toBe(
      '<span class="search-semantic-segment">hello world</span>'
    );
  });

  it('highlights only the matched query words for a keyword segment, case-insensitively', () => {
    expect(highlightClassifiedText('Hello World', 'keyword', 'world')).toBe(
      'Hello <span class="search-keyword-match">World</span>'
    );
  });

  it('wraps the whole text for a keyword segment when the query has no usable words', () => {
    expect(highlightClassifiedText('Hello', 'keyword', '')).toBe(
      '<span class="search-keyword-match">Hello</span>'
    );
  });

  it('escapes text with no classification', () => {
    expect(highlightClassifiedText('<b>x</b>', undefined, 'q')).toBe('&lt;b&gt;x&lt;/b&gt;');
  });

  it('never lets the query string introduce markup into the escaped output', () => {
    const result = highlightClassifiedText('say hi', 'keyword', '<script>');
    expect(result).not.toContain('<script>');
  });
});
