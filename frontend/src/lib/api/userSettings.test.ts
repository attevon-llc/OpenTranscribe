/**
 * `userSettings.ts` covers the thin `/user-settings/recording` wrapper plus
 * `RecordingSettingsHelper`, which has real logic worth testing directly:
 * validation, and migrating/cleaning up a legacy localStorage format.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import { UserSettingsApi, RecordingSettingsHelper } from './userSettings';

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('UserSettingsApi', () => {
  it('gets, updates, and resets recording settings against the right endpoints', async () => {
    mockInstance.get.mockResolvedValue({
      data: { max_recording_duration: 60, recording_quality: 'high', auto_stop_enabled: true },
    });
    const settings = await UserSettingsApi.getRecordingSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/recording');
    expect(settings.max_recording_duration).toBe(60);

    mockInstance.put.mockResolvedValue({ data: { ...settings, max_recording_duration: 120 } });
    const updated = await UserSettingsApi.updateRecordingSettings({ max_recording_duration: 120 });
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/recording', {
      max_recording_duration: 120,
    });
    expect(updated.max_recording_duration).toBe(120);

    mockInstance.delete.mockResolvedValue({
      data: { message: 'reset', default_settings: settings },
    });
    const reset = await UserSettingsApi.resetRecordingSettings();
    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/recording');
    expect(reset.default_settings).toEqual(settings);
  });
});

describe('RecordingSettingsHelper.getQualityLabel / getDurationLabel', () => {
  it('maps known qualities to their labels and passes through unknown ones', () => {
    expect(RecordingSettingsHelper.getQualityLabel('high')).toBe('High (128 kbps)');
    expect(RecordingSettingsHelper.getQualityLabel('bogus')).toBe('bogus');
  });

  it('formats duration under an hour in minutes, and singular/plural hours above it', () => {
    expect(RecordingSettingsHelper.getDurationLabel(30)).toBe('30 minutes');
    expect(RecordingSettingsHelper.getDurationLabel(60)).toBe('1 hour');
    expect(RecordingSettingsHelper.getDurationLabel(120)).toBe('2 hours');
  });
});

describe('RecordingSettingsHelper.validateSettings', () => {
  it('accepts every valid duration and rejects an arbitrary one', () => {
    for (const d of [15, 30, 60, 120, 240, 480]) {
      expect(RecordingSettingsHelper.validateSettings({ max_recording_duration: d })).toEqual([]);
    }
    expect(RecordingSettingsHelper.validateSettings({ max_recording_duration: 45 })).toHaveLength(
      1
    );
  });

  it('rejects an invalid quality and a non-boolean auto_stop_enabled', () => {
    expect(
      // @ts-expect-error deliberately testing an invalid value
      RecordingSettingsHelper.validateSettings({ recording_quality: 'ultra' })
    ).toHaveLength(1);
    expect(
      // @ts-expect-error deliberately testing an invalid value
      RecordingSettingsHelper.validateSettings({ auto_stop_enabled: 'yes' })
    ).toHaveLength(1);
  });
});

describe('RecordingSettingsHelper.migrateFromLocalStorage', () => {
  it('maps the legacy camelCase localStorage shape to the API snake_case shape', () => {
    localStorage.setItem(
      'recordingSettings',
      JSON.stringify({
        maxRecordingDuration: 60,
        recordingQuality: 'standard',
        autoStopEnabled: false,
      })
    );

    const migrated = RecordingSettingsHelper.migrateFromLocalStorage();

    expect(migrated).toEqual({
      max_recording_duration: 60,
      recording_quality: 'standard',
      auto_stop_enabled: false,
    });
  });

  it('returns null when nothing is stored', () => {
    expect(RecordingSettingsHelper.migrateFromLocalStorage()).toBeNull();
  });

  it('returns null rather than an invalid migrated result when the stored duration is not one of the allowed values', () => {
    localStorage.setItem('recordingSettings', JSON.stringify({ maxRecordingDuration: 999 }));
    expect(RecordingSettingsHelper.migrateFromLocalStorage()).toBeNull();
  });

  it('returns null instead of throwing on corrupt JSON', () => {
    localStorage.setItem('recordingSettings', '{not json');
    expect(RecordingSettingsHelper.migrateFromLocalStorage()).toBeNull();
  });
});

describe('RecordingSettingsHelper.cleanupLocalStorage', () => {
  it('removes the legacy key', () => {
    localStorage.setItem('recordingSettings', '{}');
    RecordingSettingsHelper.cleanupLocalStorage();
    expect(localStorage.getItem('recordingSettings')).toBeNull();
  });
});
