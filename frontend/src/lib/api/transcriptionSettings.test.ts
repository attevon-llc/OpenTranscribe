import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * `groupLanguages` is the real logic in this module — a partition + two
 * independent sorts. The CRUD
 * functions get one request-shape assertion each.
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import {
  DEFAULT_TRANSCRIPTION_SETTINGS,
  isLightweightModel,
  speakerPrefill,
  speakerSubmitRange,
  type TranscriptionSystemDefaults,
  getTranscriptionSettings,
  getTranscriptionSystemDefaults,
  groupLanguages,
  resetTranscriptionSettings,
  updateTranscriptionSettings,
} from './transcriptionSettings';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('groupLanguages', () => {
  it('partitions languages into common and other by set membership', () => {
    const all = { en: 'English', fr: 'French', ja: 'Japanese' };
    const { common, other } = groupLanguages(all, ['en', 'fr']);
    expect(common.map((l) => l.code)).toEqual(['en', 'fr']);
    expect(other.map((l) => l.code)).toEqual(['ja']);
  });

  it('treats a language code missing from commonCodes as "other", not a crash', () => {
    const all = { en: 'English', xx: 'Unknownish' };
    const { common, other } = groupLanguages(all, ['en']);
    expect(common.map((l) => l.code)).toEqual(['en']);
    expect(other.map((l) => l.code)).toEqual(['xx']);
  });

  it('puts everything into "other" when commonCodes is empty', () => {
    const all = { en: 'English', fr: 'French' };
    const { common, other } = groupLanguages(all, []);
    expect(common).toEqual([]);
    expect(other.map((l) => l.code).sort()).toEqual(['en', 'fr']);
  });

  it('sorts the common group by position in commonCodes, not input order', () => {
    // Input order is en, fr, ja; commonCodes asks for ja before en before fr.
    const all = { en: 'English', fr: 'French', ja: 'Japanese' };
    const { common } = groupLanguages(all, ['ja', 'en', 'fr']);
    expect(common.map((l) => l.code)).toEqual(['ja', 'en', 'fr']);
  });

  it('sorts the other group alphabetically by name, not by code', () => {
    const all = { zz: 'Albanian', aa: 'Zulu' };
    const { other } = groupLanguages(all, []);
    expect(other.map((l) => l.name)).toEqual(['Albanian', 'Zulu']);
  });
});

describe('CRUD requests', () => {
  it('gets transcription settings from the user-settings endpoint', async () => {
    mockInstance.get.mockResolvedValue({ data: { min_speakers: 1 } });
    const result = await getTranscriptionSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/transcription');
    expect(result).toEqual({ min_speakers: 1 });
  });

  it('updates transcription settings by putting the partial payload', async () => {
    const patch = { min_speakers: 2, max_speakers: 5 };
    mockInstance.put.mockResolvedValue({ data: { ...patch } });
    const result = await updateTranscriptionSettings(patch);
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/transcription', patch);
    expect(result).toEqual(patch);
  });

  it('resets settings via DELETE and returns the reset response', async () => {
    const resetResponse = { message: 'reset', default_settings: { min_speakers: 1 } };
    mockInstance.delete.mockResolvedValue({ data: resetResponse });
    const result = await resetTranscriptionSettings();
    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/transcription');
    expect(result).toEqual(resetResponse);
  });

  it('scopes a reset to one field group through the group query param', async () => {
    mockInstance.delete.mockResolvedValue({ data: { message: 'reset' } });
    const result = await resetTranscriptionSettings('speakers');
    expect(result).toEqual({ message: 'reset' });
    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/transcription', {
      params: { group: 'speakers' },
    });
  });

  it('gets system defaults from the system-defaults endpoint', async () => {
    mockInstance.get.mockResolvedValue({ data: { min_speakers: 1, max_speakers: 20 } });
    const result = await getTranscriptionSystemDefaults();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/transcription/system-defaults');
    expect(result).toEqual({ min_speakers: 1, max_speakers: 20 });
  });
});

describe('speakerPrefill / speakerSubmitRange (#1198)', () => {
  const base = { ...DEFAULT_TRANSCRIPTION_SETTINGS, min_speakers: 3, max_speakers: 5 };
  const system = { min_speakers: 1, max_speakers: 20 } as TranscriptionSystemDefaults;

  it('starts from the saved range unless the user chose "use system defaults"', () => {
    expect(speakerPrefill({ ...base, speaker_prompt_behavior: 'always_prompt' })).toEqual({
      minSpeakers: 3,
      maxSpeakers: 5,
    });
    expect(speakerPrefill({ ...base, speaker_prompt_behavior: 'use_custom' })).toEqual({
      minSpeakers: 3,
      maxSpeakers: 5,
    });
    expect(speakerPrefill({ ...base, speaker_prompt_behavior: 'use_defaults' })).toEqual({
      minSpeakers: null,
      maxSpeakers: null,
    });
    expect(speakerPrefill(null)).toEqual({ minSpeakers: null, maxSpeakers: null });
  });

  it('sends the SYSTEM range for blank fields under "use system defaults"', () => {
    const settings = { ...base, speaker_prompt_behavior: 'use_defaults' as const };
    expect(
      speakerSubmitRange(settings, system, { minSpeakers: null, maxSpeakers: null }, null)
    ).toEqual({ minSpeakers: 1, maxSpeakers: 20 });
    // A value the user typed still wins.
    expect(
      speakerSubmitRange(settings, system, { minSpeakers: 2, maxSpeakers: null }, null)
    ).toEqual({ minSpeakers: 2, maxSpeakers: 20 });
  });

  it('injects nothing when a fixed count replaces the range', () => {
    const settings = { ...base, speaker_prompt_behavior: 'use_defaults' as const };
    expect(
      speakerSubmitRange(settings, system, { minSpeakers: null, maxSpeakers: null }, 4)
    ).toEqual({ minSpeakers: null, maxSpeakers: null });
  });

  it('leaves blanks blank elsewhere, so the server applies the saved range', () => {
    const settings = { ...base, speaker_prompt_behavior: 'always_prompt' as const };
    expect(
      speakerSubmitRange(settings, system, { minSpeakers: null, maxSpeakers: null }, null)
    ).toEqual({ minSpeakers: null, maxSpeakers: null });
  });
});

describe('isLightweightModel', () => {
  it('recognises the models the server routes to the CPU worker', () => {
    expect(['tiny', 'tiny.en', 'base', 'base.en'].every(isLightweightModel)).toBe(true);
    expect(isLightweightModel('large-v3-turbo')).toBe(false);
    expect(isLightweightModel(null)).toBe(false);
  });
});
