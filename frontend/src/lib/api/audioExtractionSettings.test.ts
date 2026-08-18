/**
 * `audioExtractionSettings.ts` — thin wrapper over `/user-settings/audio-extraction`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import {
  getAudioExtractionSettings,
  updateAudioExtractionSettings,
} from './audioExtractionSettings';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getAudioExtractionSettings', () => {
  it('fetches and returns the settings', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        auto_extract_enabled: true,
        extraction_threshold_mb: 500,
        remember_choice: false,
        show_modal: true,
      },
    });
    const settings = await getAudioExtractionSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/audio-extraction');
    expect(settings.extraction_threshold_mb).toBe(500);
  });
});

describe('updateAudioExtractionSettings', () => {
  it('PUTs a partial update and returns the merged settings', async () => {
    mockInstance.put.mockResolvedValue({
      data: {
        auto_extract_enabled: false,
        extraction_threshold_mb: 500,
        remember_choice: false,
        show_modal: true,
      },
    });
    const settings = await updateAudioExtractionSettings({ auto_extract_enabled: false });
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/audio-extraction', {
      auto_extract_enabled: false,
    });
    expect(settings.auto_extract_enabled).toBe(false);
  });
});
