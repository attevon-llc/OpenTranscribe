import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { searchStore, SEARCH_SOURCES } from '$stores/search';
import {
  applyFilterDetail,
  buildApiParams,
  buildPageUrlParams,
  parseSearchUrl,
  parseSourcesParam,
  resetStoreFilters,
  searchCacheKey,
  serializeApiParams,
} from './searchParams';

const sp = (q: string) => new URLSearchParams(q);
const state = () => get(searchStore);

beforeEach(() => searchStore.reset());

describe('parseSourcesParam', () => {
  it('reads repeated sources= and drops unknown values', () => {
    expect(parseSourcesParam(sp('sources=summary&sources=bogus&sources=title'))).toEqual([
      'summary',
      'title',
    ]);
  });
  it('treats an all-invalid list as no override', () => {
    expect(parseSourcesParam(sp('sources=bogus'))).toBeNull();
  });
  it('maps the legacy type= scheme', () => {
    expect(parseSourcesParam(sp('type=summaries'))).toEqual(['summary']);
    expect(parseSourcesParam(sp('type=transcripts'))).toEqual(['content', 'title', 'speaker']);
    expect(parseSourcesParam(sp('type=all'))).toEqual([...SEARCH_SOURCES]);
    expect(parseSourcesParam(sp('type=other'))).toBeNull();
    expect(parseSourcesParam(sp(''))).toBeNull();
  });
});

describe('parseSearchUrl', () => {
  it('defaults every field', () => {
    expect(parseSearchUrl(sp(''))).toEqual({
      query: '',
      page: 1,
      sort: 'relevance',
      sortOrder: 'desc',
      mode: 'hybrid',
      sources: null,
      speakers: [],
      tags: [],
      collection: null,
    });
  });
  it('reads them back', () => {
    const r = parseSearchUrl(
      sp('q=hi&page=3&sort=duration&sort_order=asc&mode=keyword&tags=a&tags=b&collection=c1')
    );
    expect(r).toMatchObject({
      query: 'hi',
      page: 3,
      sort: 'duration',
      sortOrder: 'asc',
      mode: 'keyword',
      tags: ['a', 'b'],
      collection: 'c1',
    });
  });
});

describe('buildPageUrlParams', () => {
  it('omits every default, including the all-sources selection', () => {
    expect(buildPageUrlParams('hello', 1, state()).toString()).toBe('q=hello');
  });
  it('carries non-defaults, and a narrowed source set', () => {
    searchStore.setSort('duration', 'asc');
    searchStore.setSources(['summary']);
    searchStore.setTags(['t']);
    const p = buildPageUrlParams('q', 2, state());
    expect(p.get('page')).toBe('2');
    expect(p.get('sort')).toBe('duration');
    expect(p.get('sort_order')).toBe('asc');
    expect(p.getAll('sources')).toEqual(['summary']);
    expect(p.getAll('tags')).toEqual(['t']);
  });
});

describe('buildApiParams', () => {
  it('always sends sources and uses collection_uuid, never collection_id', () => {
    searchStore.setCollectionId('uuid-1');
    const p = buildApiParams('q', 1, state());
    expect(p.sources).toEqual([...SEARCH_SOURCES]);
    expect(p.collection_uuid).toBe('uuid-1');
    expect(p).not.toHaveProperty('collection_id');
  });
  it('sends duration bounds only when set, including zero', () => {
    searchStore.setDurationRange({ min: 0, max: null });
    const p = buildApiParams('q', 1, state());
    expect(p.min_duration).toBe(0);
    expect(p).not.toHaveProperty('max_duration');
  });
});

describe('serializeApiParams', () => {
  it('repeats arrays and skips undefined', () => {
    expect(serializeApiParams({ a: ['x', 'y'], b: undefined, c: 1 })).toBe('a=x&a=y&c=1');
  });
});

describe('searchCacheKey', () => {
  it('is independent of source order but sensitive to filters', () => {
    searchStore.setSources(['title', 'content']);
    const a = searchCacheKey('q', 1, state());
    searchStore.setSources(['content', 'title']);
    expect(searchCacheKey('q', 1, state())).toBe(a);
    searchStore.setTags(['t']);
    expect(searchCacheKey('q', 1, state())).not.toBe(a);
  });
});

describe('applyFilterDetail / resetStoreFilters', () => {
  it('applies only the keys present, formatting dates as ISO days', () => {
    searchStore.setTags(['keep']);
    applyFilterDetail({ dates: { from: new Date('2026-03-04T00:00:00Z'), to: null } });
    expect(state().dateFrom).toBe('2026-03-04');
    expect(state().dateTo).toBe('');
    expect(state().selectedTags).toEqual(['keep']);
  });
  it('reset clears them all', () => {
    applyFilterDetail({
      tags: ['a'],
      speaker: ['s'],
      fileTypes: ['audio'],
      language: 'en',
      statuses: ['completed'],
    });
    resetStoreFilters();
    const s = state();
    expect([s.selectedTags, s.selectedSpeakers, s.selectedFileTypes, s.selectedStatuses]).toEqual([
      [],
      [],
      [],
      [],
    ]);
    expect(s.selectedLanguage).toBeNull();
  });
});
