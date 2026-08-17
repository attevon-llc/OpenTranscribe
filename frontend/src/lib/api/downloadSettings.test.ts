/**
 * `downloadSettings.ts` is a thin CRUD wrapper around `/user-settings/download`
 * — these tests pin request shape and response pass-through.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('../axios', () => ({ default: mockInstance }));

import {
  getDownloadSettings,
  updateDownloadSettings,
  resetDownloadSettings,
  getDownloadSystemDefaults,
} from './downloadSettings';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('downloadSettings', () => {
  it('gets the user settings', async () => {
    const settings = { video_quality: '1080p', audio_only: false, audio_quality: 'best' };
    mockInstance.get.mockResolvedValue({ data: settings });

    const result = await getDownloadSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/download');
    expect(result).toEqual(settings);
  });

  it('PUTs a partial update and returns the resolved settings', async () => {
    const updated = { video_quality: '720p', audio_only: false, audio_quality: 'best' };
    mockInstance.put.mockResolvedValue({ data: updated });

    const result = await updateDownloadSettings({ video_quality: '720p' });
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/download', {
      video_quality: '720p',
    });
    expect(result).toEqual(updated);
  });

  it('resets to system defaults via DELETE', async () => {
    const resetResponse = {
      message: 'reset',
      default_settings: { video_quality: 'best', audio_only: false, audio_quality: 'best' },
    };
    mockInstance.delete.mockResolvedValue({ data: resetResponse });

    const result = await resetDownloadSettings();
    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/download');
    expect(result).toEqual(resetResponse);
  });

  it('fetches the system defaults', async () => {
    const defaults = {
      video_quality: 'best',
      audio_only: false,
      audio_quality: 'best',
      available_video_qualities: { best: 'Best', '1080p': '1080p' },
      available_audio_qualities: { best: 'Best' },
    };
    mockInstance.get.mockResolvedValue({ data: defaults });

    const result = await getDownloadSystemDefaults();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/download/system-defaults');
    expect(result).toEqual(defaults);
  });
});
