import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * `groupLanguages` is the real logic in this module — a partition + two
 * independent sorts. `getSpeakerBehaviorLabel`/`Description` are simple maps
 * whose only branch worth testing is the raw-value fallback. The CRUD
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
  getSpeakerBehaviorDescription,
  getSpeakerBehaviorLabel,
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

describe('getSpeakerBehaviorLabel', () => {
  it('returns the known label for each valid behavior', () => {
    expect(getSpeakerBehaviorLabel('always_prompt')).toBe('Always show speaker settings');
    expect(getSpeakerBehaviorLabel('use_defaults')).toBe('Use system defaults');
    expect(getSpeakerBehaviorLabel('use_custom')).toBe('Use my saved settings');
  });

  it('falls back to echoing the raw value for an unrecognized behavior', () => {
    // Cast past the union to simulate a server value the client doesn't know about.
    expect(getSpeakerBehaviorLabel('unknown_behavior' as never)).toBe('unknown_behavior');
  });
});

describe('getSpeakerBehaviorDescription', () => {
  it('returns the known description for each valid behavior', () => {
    expect(getSpeakerBehaviorDescription('use_defaults')).toBe(
      'Skip settings and use system MIN/MAX_SPEAKERS values'
    );
  });

  it('falls back to an empty string for an unrecognized behavior, not the raw value', () => {
    // Unlike getSpeakerBehaviorLabel, the description fallback is '' rather than the raw value.
    expect(getSpeakerBehaviorDescription('unknown_behavior' as never)).toBe('');
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

  it('gets system defaults from the system-defaults endpoint', async () => {
    mockInstance.get.mockResolvedValue({ data: { min_speakers: 1, max_speakers: 20 } });
    const result = await getTranscriptionSystemDefaults();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/transcription/system-defaults');
    expect(result).toEqual({ min_speakers: 1, max_speakers: 20 });
  });
});
