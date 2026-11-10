/**
 * The single-file "extract audio" path hands the wizard's options to a background job and
 * then resets the wizard. The options must be read BEFORE the reset: resetAllState() clears
 * the voiceprint consent, so reading them afterwards silently dropped it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
vi.mock('$stores/uploads', () => ({
  uploadsStore: { addFile: vi.fn(), addFiles: vi.fn(), addRecording: vi.fn() },
}));
vi.mock('$lib/services/configService', () => ({
  loadProtectedMediaAuthConfig: vi.fn().mockResolvedValue(undefined),
  getAuthConfigForUrl: vi.fn().mockReturnValue(null),
}));
vi.mock('$lib/api/audioExtractionSettings', () => ({
  getAudioExtractionSettings: vi.fn().mockResolvedValue({
    auto_extract_enabled: true,
    extraction_threshold_mb: 100,
    remember_choice: false,
    show_modal: true,
  }),
}));
vi.mock('$lib/api/tags', () => ({ listTags: vi.fn().mockResolvedValue([]) }));
vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: { getActiveLocalModel: vi.fn().mockResolvedValue({ active_model: 'large-v3' }) },
}));
const mockExtractAndQueue = vi.hoisted(() => vi.fn().mockResolvedValue(true));
vi.mock('$lib/upload/bulkExtraction', () => ({
  extractAndQueue: mockExtractAndQueue,
  startBulkExtraction: vi.fn(),
}));

import FileUploader from './FileUploader.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mockAxios.get.mockResolvedValue({ data: [] });
  mockAxios.post.mockResolvedValue({ data: {} });
});

describe('single-file audio extraction', () => {
  it('forwards the voiceprint consent and the chosen tags to the extraction job', async () => {
    localStorage.setItem(
      'opentr:uploadPreviousValues',
      JSON.stringify({ collectionIds: [], collectionNames: [], tagNames: ['board'] })
    );
    const { container } = render(FileUploader);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull());

    const video = new File(['x'], 'clip.mp4', { type: 'video/mp4' });
    Object.defineProperty(video, 'size', { configurable: true, value: 150 * 1024 * 1024 });
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { configurable: true, value: [video] });
    await fireEvent.change(input);

    const clickNext = async () => {
      await waitFor(() =>
        expect(container.querySelector('.nav-next:not([disabled])')).not.toBeNull()
      );
      await fireEvent.click(container.querySelector('.nav-next') as HTMLElement);
    };
    for (let i = 0; i < 12 && !container.querySelector('#voiceprint-consent'); i++) {
      await clickNext();
    }
    await waitFor(() => expect(container.querySelector('#voiceprint-consent')).not.toBeNull());
    await fireEvent.click(container.querySelector('#voiceprint-consent') as HTMLElement);
    for (let i = 0; i < 12 && !container.querySelector('.nav-submit'); i++) {
      await clickNext();
    }
    await fireEvent.click(container.querySelector('.nav-submit') as HTMLElement);

    expect(mockExtractAndQueue).toHaveBeenCalledTimes(1);
    const [, params, organize] = mockExtractAndQueue.mock.calls[0];
    expect(params.voiceprintConsent).toBe('granted');
    expect(organize.tagNames).toEqual(['board']);
  });
});
