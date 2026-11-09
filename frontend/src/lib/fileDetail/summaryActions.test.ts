import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockToast = vi.hoisted(() => ({ info: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import { fetchSummaryData, startSummary } from './summaryActions';

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('startSummary', () => {
  it('posts the options and announces queued topic suggestions', async () => {
    mockAxios.post.mockResolvedValue({ data: { topic_extraction: 'queued' } });

    await startSummary('f1', { forceRegenerate: true, promptUuid: 'p1' });

    expect(mockAxios.post.mock.calls[0][0]).toBe('/files/f1/summarize');
    expect(mockAxios.post.mock.calls[0][1]).toEqual({ force_regenerate: true, prompt_uuid: 'p1' });
    expect(mockToast.info).toHaveBeenCalledWith('fileDetail.topicSuggestionsQueued', 6000);
  });

  it('announces nothing when no topic extraction was queued, and rethrows a failure', async () => {
    mockAxios.post.mockResolvedValueOnce({ data: {} });
    await startSummary('f1');
    expect(mockToast.info).not.toHaveBeenCalled();

    mockAxios.post.mockRejectedValueOnce(new Error('down'));
    await expect(startSummary('f1')).rejects.toThrow('down');
  });
});

describe('fetchSummaryData', () => {
  it('returns the stored summary', async () => {
    mockAxios.get.mockResolvedValue({ data: { summary_data: { bluf: 'x' } } });
    expect(await fetchSummaryData('f1')).toEqual({ bluf: 'x' });
    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1/summary');
  });

  it('is silent for "no summary yet" and toasts any other failure', async () => {
    mockAxios.get.mockRejectedValueOnce({ response: { status: 404 } });
    expect(await fetchSummaryData('f1')).toBeUndefined();
    expect(mockToast.error).not.toHaveBeenCalled();

    mockAxios.get.mockRejectedValueOnce({ response: { status: 500 } });
    expect(await fetchSummaryData('f1')).toBeUndefined();
    expect(mockToast.error).toHaveBeenCalledWith('fileDetail.failedToLoadSummary', 5000);
  });
});
