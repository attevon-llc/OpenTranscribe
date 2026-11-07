/** The deployment-wide VAD defaults card: loads, edits and saves the four values. */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import TranscriptionDefaultsCard from './TranscriptionDefaultsCard.svelte';

const DEFAULTS = {
  vad_threshold: 0.5,
  vad_min_silence_ms: 2000,
  vad_min_speech_ms: 250,
  vad_speech_pad_ms: 400,
};

describe('TranscriptionDefaultsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: DEFAULTS });
  });

  it('loads the deployment defaults', async () => {
    render(TranscriptionDefaultsCard);
    expect(await screen.findByDisplayValue('2000')).toBeTruthy();
    expect(axiosInstance.get).toHaveBeenCalledWith('/admin/transcription-defaults');
  });

  it('saves a changed silence gate', async () => {
    vi.mocked(axiosInstance.put).mockResolvedValue({
      data: { ...DEFAULTS, vad_min_silence_ms: 1500 },
    });
    render(TranscriptionDefaultsCard);
    const silence = await screen.findByDisplayValue('2000');
    await fireEvent.input(silence, { target: { value: '1500' } });
    await fireEvent.click(screen.getByText('settings.transcriptionDefaults.save'));
    await waitFor(() =>
      expect(axiosInstance.put).toHaveBeenCalledWith(
        '/admin/transcription-defaults',
        expect.objectContaining({ vad_min_silence_ms: 1500 })
      )
    );
    expect(toastStore.success).toHaveBeenCalledWith('settings.transcriptionDefaults.saved');
  });
});
