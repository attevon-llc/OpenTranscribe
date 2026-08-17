import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * The client is transport-only, so these tests assert the wire shape each method
 * produces (path, method, body) and that `response.data` comes back unchanged.
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
  getSpeakerAttributeSettings,
  updateSpeakerAttributeSettings,
  resetSpeakerAttributeSettings,
  getSpeakerAttributeSystemDefaults,
} from './speakerAttributeSettings';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('getSpeakerAttributeSettings', () => {
  it('reads the user settings and returns them unchanged', async () => {
    const settings = {
      detection_enabled: true,
      gender_detection_enabled: false,
      show_attributes_on_cards: true,
    };
    mockInstance.get.mockResolvedValue({ data: settings });

    const result = await getSpeakerAttributeSettings();

    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/speaker-attributes');
    expect(result).toEqual(settings);
  });
});

describe('updateSpeakerAttributeSettings', () => {
  it('puts a partial update and returns the server row', async () => {
    const updated = {
      detection_enabled: false,
      gender_detection_enabled: false,
      show_attributes_on_cards: true,
    };
    mockInstance.put.mockResolvedValue({ data: updated });

    const result = await updateSpeakerAttributeSettings({ detection_enabled: false });

    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/speaker-attributes', {
      detection_enabled: false,
    });
    expect(result).toEqual(updated);
  });
});

describe('resetSpeakerAttributeSettings', () => {
  it('deletes the user override with no body and returns nothing', async () => {
    const result = await resetSpeakerAttributeSettings();

    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/speaker-attributes');
    expect(result).toBeUndefined();
  });
});

describe('getSpeakerAttributeSystemDefaults', () => {
  it('reads the system defaults and returns them unchanged', async () => {
    const defaults = {
      detection_enabled: true,
      gender_detection_enabled: true,
      show_attributes_on_cards: false,
    };
    mockInstance.get.mockResolvedValue({ data: defaults });

    const result = await getSpeakerAttributeSystemDefaults();

    expect(mockInstance.get).toHaveBeenCalledWith(
      '/user-settings/speaker-attributes/system-defaults'
    );
    expect(result).toEqual(defaults);
  });
});
