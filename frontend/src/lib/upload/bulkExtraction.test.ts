import { describe, it, expect, vi } from 'vitest';

const addExtractedAudio = vi.hoisted(() => vi.fn());
vi.mock('$stores/uploads', () => ({ uploadsStore: { addExtractedAudio } }));
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
vi.mock('$lib/services/audioExtractionService', () => ({
  audioExtractionService: {
    extractAudio: vi.fn().mockResolvedValue({
      blob: new Blob(['a']),
      filename: 'a.mp3',
      metadata: { compressionRatio: 3 },
    }),
  },
}));

import { extractAndQueue } from './bulkExtraction';

describe('extractAndQueue', () => {
  it('queues the extracted audio with the wizard collections and tags', async () => {
    const params = { voiceprintConsent: 'granted' as const };
    await extractAndQueue(new File(['x'], 'v.mp4'), params as never, {
      collectionIds: ['c1'],
      tagNames: ['t1'],
    });
    expect(addExtractedAudio).toHaveBeenCalledWith(
      expect.any(Blob),
      'a.mp3',
      expect.anything(),
      3,
      params,
      ['c1'],
      ['t1']
    );
  });
});
