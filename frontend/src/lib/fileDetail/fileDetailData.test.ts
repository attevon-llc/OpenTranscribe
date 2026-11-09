import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockGetAISuggestions = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/suggestions', () => ({ getAISuggestions: mockGetAISuggestions }));

const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
import {
  fetchAISuggestions,
  fetchFileAnalytics,
  getFileDetail,
  mergeTranscriptRefresh,
  paginationFrom,
  readRedactionState,
  sawMaskedSpans,
  startRedactionScan,
} from './fileDetailData';

beforeEach(() => {
  vi.clearAllMocks();
  apiCache.invalidate('prefetch:file:');
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('getFileDetail', () => {
  it('consumes a warm prefetch entry once, without a request', async () => {
    apiCache.set(cacheKey.fileDetail('f1'), { uuid: 'f1' }, CacheTTL.FILES);

    expect(await getFileDetail('f1', { fromPrefetch: true, showOriginal: false })).toEqual({
      uuid: 'f1',
    });
    expect(mockAxios.get).not.toHaveBeenCalled();
    expect(apiCache.get(cacheKey.fileDetail('f1'))).toBeNull();
  });

  it('never serves the masked prefetch to a show-original read', async () => {
    apiCache.set(cacheKey.fileDetail('f1'), { uuid: 'masked' }, CacheTTL.FILES);
    mockAxios.get.mockResolvedValue({ data: { uuid: 'original' } });

    const data = await getFileDetail('f1', { fromPrefetch: true, showOriginal: true });

    expect(data).toEqual({ uuid: 'original' });
    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1', { params: { redact: false } });
  });
});

describe('response mapping', () => {
  it('reads the redaction state with safe defaults', () => {
    expect(readRedactionState({})).toEqual({
      redactionPending: false,
      redactionStatus: '',
      redactionEnabled: false,
    });
    expect(
      readRedactionState({
        redaction_pending: true,
        redaction_status: 'done',
        redaction_enabled: 1,
      })
    ).toEqual({ redactionPending: true, redactionStatus: 'done', redactionEnabled: false });
  });

  it('sees masked spans only on a masked read', () => {
    const data = { transcript_segments: [{ redactions: [] }, { redactions: [{}] }] };
    expect(sawMaskedSpans(data, false)).toBe(true);
    expect(sawMaskedSpans(data, true)).toBe(false);
    expect(sawMaskedSpans({ transcript_segments: [{ redactions: [] }, null] }, false)).toBe(false);
  });

  it('defaults the pagination fields', () => {
    expect(paginationFrom({})).toEqual({
      totalSegments: 0,
      totalSpeakerSegments: 0,
      segmentLimit: 500,
    });
  });

  it('merges a transcript refresh, keeping unrelated fields and absent optional ones', () => {
    const file = { uuid: 'f1', title: 'Edited', asr_model: 'large', transcript_segments: [1] };
    const next = mergeTranscriptRefresh(file, {
      transcript_segments: [{}],
      analytics: { a: 1 },
      language: 'fr',
    });
    expect(next).toMatchObject({
      uuid: 'f1',
      title: 'Edited',
      asr_model: 'large',
      language: 'fr',
      grouped_segments: [],
      speakers: [],
      analytics: { a: 1 },
    });
    expect(next.transcript_segments).toEqual([{}]);
    expect(next).not.toBe(file);
  });
});

describe('fetchFileAnalytics', () => {
  it('returns the analytics, or null when the request fails', async () => {
    mockAxios.get.mockResolvedValueOnce({ data: { analytics: { talk: 1 } } });
    expect(await fetchFileAnalytics('f1')).toEqual({ analytics: { talk: 1 } });
    mockAxios.get.mockRejectedValueOnce(new Error('x'));
    expect(await fetchFileAnalytics('f1')).toBeNull();
    expect(mockToast.error).not.toHaveBeenCalled();
  });
});

describe('startRedactionScan', () => {
  it('reports success', async () => {
    mockAxios.post.mockResolvedValue({ data: [{ success: true }] });
    expect(await startRedactionScan('f1')).toBe(true);
    expect(mockAxios.post).toHaveBeenCalledWith('/files/management/bulk-action', {
      file_uuids: ['f1'],
      action: 'redact',
    });
    expect(mockToast.success).toHaveBeenCalledWith('settings.contentRedaction.redactStarted');
  });

  it("shows the server's reason for a refusal", async () => {
    mockAxios.post.mockRejectedValue({
      response: { data: { detail: 'redaction is not enabled' } },
    });
    expect(await startRedactionScan('f1')).toBe(false);
    expect(mockToast.error).toHaveBeenCalledWith('redaction is not enabled');
  });

  it('falls back to a generic message for an unsuccessful result', async () => {
    mockAxios.post.mockResolvedValue({ data: [{ success: false }] });
    expect(await startRedactionScan('f1')).toBe(false);
    expect(mockToast.error).toHaveBeenCalledWith('settings.contentRedaction.redactTriggerFailed');
  });
});

describe('fetchAISuggestions', () => {
  it('returns stored suggestions with empty-list defaults', async () => {
    mockGetAISuggestions.mockResolvedValue({ status: 'pending', tags: [{ name: 'x' }] });
    expect(await fetchAISuggestions('f1')).toEqual({ tags: [{ name: 'x' }], collections: [] });
  });

  it('returns null for rejected, none, or a failed read', async () => {
    mockGetAISuggestions.mockResolvedValueOnce({ status: 'rejected', tags: [{ name: 'x' }] });
    expect(await fetchAISuggestions('f1')).toBeNull();
    mockGetAISuggestions.mockResolvedValueOnce(null);
    expect(await fetchAISuggestions('f1')).toBeNull();
    mockGetAISuggestions.mockRejectedValueOnce(new Error('404'));
    expect(await fetchAISuggestions('f1')).toBeNull();
  });
});
