import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({
  api: {
    getRecordingSettings: vi.fn(),
    updateRecordingSettings: vi.fn(),
    resetRecordingSettings: vi.fn(),
  },
  validate: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn() },
  handleApiError: vi.fn(),
}));
vi.mock('$lib/api/userSettings', () => ({
  UserSettingsApi: h.api,
  RecordingSettingsHelper: { validateSettings: h.validate },
}));
vi.mock('$stores/toast', () => ({ toastStore: h.toast }));
vi.mock('$lib/utils/apiError', () => ({
  getErrorMessage: (e: unknown, fallback: string) => (e as Error).message || fallback,
  handleApiError: h.handleApiError,
}));

import { loadRecording, resetRecording, saveRecording } from './recordingSettings';

const t = (k: string) => `T:${k}`;
const settings = {
  max_recording_duration: 60,
  recording_quality: 'high',
  auto_stop_enabled: true,
} as never;

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  h.validate.mockReturnValue([]);
});

describe('loadRecording', () => {
  it('returns the saved settings', async () => {
    h.api.getRecordingSettings.mockResolvedValue(settings);
    expect(await loadRecording(t)).toBe(settings);
  });

  it('reports through handleApiError (lifecycle 403s stay quiet) and returns null', async () => {
    h.api.getRecordingSettings.mockRejectedValue(new Error('403'));
    expect(await loadRecording(t)).toBeNull();
    expect(h.handleApiError).toHaveBeenCalledWith(
      expect.any(Error),
      'T:settings.toast.recordingSettingsSaveFailed'
    );
    expect(h.toast.error).not.toHaveBeenCalled();
  });
});

describe('saveRecording', () => {
  it('refuses invalid settings with the first validation error and does not call the API', async () => {
    h.validate.mockReturnValue(['too long', 'other']);
    expect(await saveRecording(settings, t)).toBe(false);
    expect(h.toast.error).toHaveBeenCalledWith('too long');
    expect(h.api.updateRecordingSettings).not.toHaveBeenCalled();
  });

  it('saves and toasts success', async () => {
    h.api.updateRecordingSettings.mockResolvedValue(undefined);
    expect(await saveRecording(settings, t)).toBe(true);
    expect(h.api.updateRecordingSettings).toHaveBeenCalledWith(settings);
    expect(h.toast.success).toHaveBeenCalledWith('T:settings.toast.recordingSettingsSaved');
  });

  it('returns false with the server message on failure', async () => {
    h.api.updateRecordingSettings.mockRejectedValue(new Error('nope'));
    expect(await saveRecording(settings, t)).toBe(false);
    expect(h.toast.error).toHaveBeenCalledWith('nope');
  });
});

describe('resetRecording', () => {
  it('resets, reloads the form, then toasts', async () => {
    const order: string[] = [];
    h.api.resetRecordingSettings.mockImplementation(async () => order.push('reset'));
    const reload = vi.fn(async () => void order.push('reload'));
    expect(await resetRecording(t, reload)).toBe(true);
    expect(order).toEqual(['reset', 'reload']);
    expect(h.toast.success).toHaveBeenCalledWith('T:settings.toast.recordingSettingsReset');
  });

  it('does not reload when the reset itself fails', async () => {
    h.api.resetRecordingSettings.mockRejectedValue(new Error('boom'));
    const reload = vi.fn();
    expect(await resetRecording(t, reload)).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    expect(h.toast.error).toHaveBeenCalledWith('boom');
  });
});
