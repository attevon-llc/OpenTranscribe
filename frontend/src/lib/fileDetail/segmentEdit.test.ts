import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ put: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

import { clearProcessedVideoCache, saveSegmentText, segmentSaveErrorKey } from './segmentEdit';

beforeEach(() => vi.clearAllMocks());

describe('saveSegmentText', () => {
  it('PUTs only the text and resolves the stored row', async () => {
    mockAxios.put.mockResolvedValue({ data: { text: 'stored' } });

    expect(await saveSegmentText('f1', 's1', 'typed')).toEqual({ text: 'stored' });
    expect(mockAxios.put).toHaveBeenCalledWith('/files/f1/transcript/segments/s1', {
      text: 'typed',
    });
  });

  it('resolves null for an empty response and rejects on failure', async () => {
    mockAxios.put.mockResolvedValueOnce({ data: '' });
    expect(await saveSegmentText('f1', 's1', 'x')).toBeNull();
    mockAxios.put.mockRejectedValueOnce({ response: { status: 422 } });
    await expect(saveSegmentText('f1', 's1', 'x')).rejects.toEqual({ response: { status: 422 } });
  });
});

describe('segmentSaveErrorKey', () => {
  it.each([
    [405, 'fileDetail.transcriptEditingNotSupported'],
    [404, 'fileDetail.transcriptSegmentNotFound'],
    [422, 'fileDetail.invalidSegmentData'],
    [500, 'fileDetail.failedToSaveSegment'],
    [undefined, 'fileDetail.failedToSaveSegment'],
  ])('status %s -> %s', (status, key) => {
    expect(segmentSaveErrorKey(status)).toBe(key);
  });
});

describe('clearProcessedVideoCache', () => {
  it('deletes the cache and swallows a failure with a warning', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockAxios.delete.mockRejectedValue(new Error('x'));

    clearProcessedVideoCache('f1');
    await Promise.resolve();
    await Promise.resolve();

    expect(mockAxios.delete).toHaveBeenCalledWith('/files/f1/cache');
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});
