import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({
  get: vi.fn(),
  getOrFetch: vi.fn(async (_key: unknown, fetcher: () => unknown) => fetcher()),
  fetchMetadataFilters: vi.fn(),
}));
vi.mock('$lib/axios', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/axios')>()),
  default: { get: h.get },
}));
vi.mock('$lib/api/tags', () => ({ listTags: vi.fn() }));
vi.mock('$lib/api/metadataFilters', () => ({ fetchMetadataFilters: h.fetchMetadataFilters }));
vi.mock('$lib/apiCache', () => ({
  apiCache: { getOrFetch: h.getOrFetch },
  cacheKey: {
    tags: () => 'tags',
    owners: () => 'owners',
    speakers: (q?: string) => `speakers:${q ?? ''}`,
  },
  CacheTTL: { TAGS: 1, OWNERS: 2, SPEAKERS: 3 },
}));

import {
  SPEAKER_FILTER_PAGE_SIZE,
  describeError,
  latestFacetLoad,
  loadLibraryMetadata,
  loadOwners,
  loadSpeakers,
  ownerLabel,
  speakerLabel,
} from './filterData';

beforeEach(() => vi.clearAllMocks());

describe('labels', () => {
  it('never falls back to a raw email', () => {
    expect(ownerLabel({ uuid: 'u', full_name: null, masked_email: 'a***@x.io' })).toBe('a***@x.io');
    expect(ownerLabel({ uuid: 'u', full_name: 'Ann', masked_email: 'a***@x.io' })).toBe('Ann');
    expect(speakerLabel({ name: 'SPEAKER_00', display_name: 'Bo' })).toBe('Bo');
    expect(speakerLabel({ name: 'SPEAKER_00', display_name: null })).toBe('SPEAKER_00');
  });

  it('renders a non-Error throw', () => {
    expect(describeError(new Error('boom'))).toBe('boom');
    expect(describeError('plain')).toBe('plain');
  });
});

describe('loadSpeakers', () => {
  it('asks for one more than it shows, so a cut list is detectable (#743b)', async () => {
    const rows = Array.from({ length: SPEAKER_FILTER_PAGE_SIZE + 1 }, (_, i) => ({
      uuid: `s${i}`,
      name: 'n',
    }));
    h.get.mockResolvedValue({ data: rows });
    const result = await loadSpeakers(' bo ');
    expect(h.get).toHaveBeenCalledWith('/speakers', {
      params: {
        for_filter: true,
        q: 'bo',
        include_unnamed: true,
        limit: SPEAKER_FILTER_PAGE_SIZE + 1,
      },
    });
    expect(result.truncated).toBe(true);
    expect(result.speakers).toHaveLength(SPEAKER_FILTER_PAGE_SIZE);
  });

  it('is not truncated when the server returned everything, and tolerates a non-array body', async () => {
    h.get.mockResolvedValue({ data: [{ uuid: 'a', name: 'n' }] });
    expect(await loadSpeakers()).toMatchObject({ truncated: false });
    h.get.mockResolvedValue({ data: { detail: 'x' } });
    expect(await loadSpeakers()).toEqual({ speakers: [], truncated: false });
  });
});

describe('loadOwners / loadLibraryMetadata', () => {
  it('returns [] for a non-array owners body', async () => {
    h.get.mockResolvedValue({ data: null });
    expect(await loadOwners()).toEqual([]);
  });

  it('floors/ceils duration bounds and keeps at least a minute of range', async () => {
    h.fetchMetadataFilters.mockResolvedValue({
      duration: { min: 12.7, max: 12.9 },
      languages: ['en'],
    });
    expect(await loadLibraryMetadata()).toEqual({
      durationBounds: { min: 12, max: 72 },
      languages: ['en'],
    });
  });

  it('reports no bounds when the library has no duration, and propagates failure', async () => {
    h.fetchMetadataFilters.mockResolvedValue({ languages: 'nope' });
    expect(await loadLibraryMetadata()).toEqual({ durationBounds: null, languages: [] });
    h.fetchMetadataFilters.mockRejectedValue(new Error('500'));
    await expect(loadLibraryMetadata()).rejects.toThrow('500');
  });
});

/** A promise the test settles by hand, so two loads can finish in either order. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('latestFacetLoad', () => {
  it('passes through a single load: value, or the error with its message', async () => {
    const ok = latestFacetLoad(async (q: string) => `rows for ${q}`);
    expect(await ok('a')).toEqual({ kind: 'ok', value: 'rows for a' });
    const boom = new Error('500');
    const failing = latestFacetLoad(() => Promise.reject(boom));
    expect(await failing()).toEqual({ kind: 'error', message: '500', cause: boom });
  });

  it('an older load that settles last is superseded, whether it succeeded or failed', async () => {
    const calls = [deferred<string>(), deferred<string>(), deferred<string>()];
    let n = 0;
    const load = latestFacetLoad(() => calls[n++]!.promise);

    const first = load();
    const second = load();
    calls[1]!.resolve('new');
    calls[0]!.resolve('stale');
    expect(await second).toEqual({ kind: 'ok', value: 'new' });
    expect(await first).toEqual({ kind: 'superseded' });

    const third = load();
    calls[2]!.reject(new Error('late failure'));
    expect(await third).toMatchObject({ kind: 'error' });
  });

  it('a superseded load that FAILS never reports an error', async () => {
    const calls = [deferred<string>(), deferred<string>()];
    let n = 0;
    const load = latestFacetLoad(() => calls[n++]!.promise);
    const first = load();
    const second = load();
    calls[1]!.resolve('fresh');
    calls[0]!.reject(new Error('Network Error'));
    expect(await first).toEqual({ kind: 'superseded' });
    expect(await second).toEqual({ kind: 'ok', value: 'fresh' });
  });

  it('a cancelled request is not an error', async () => {
    const cancelled = Object.assign(new Error('canceled'), {
      code: 'ERR_CANCELED',
      name: 'CanceledError',
    });
    const load = latestFacetLoad(() => Promise.reject(cancelled));
    expect(await load()).toEqual({ kind: 'cancelled' });
  });
});
