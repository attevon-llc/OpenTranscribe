import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('$lib/api/asrSettings', () => ({
  ASRSettingsApi: {
    getStatus: vi.fn().mockResolvedValue({}),
    getProviderDisplayName: (p: string) => p,
  },
}));

const api = vi.hoisted(() => ({
  getTranscriptionSettings: vi.fn(),
  getTranscriptionSystemDefaults: vi.fn(),
  updateTranscriptionSettings: vi.fn(),
  resetTranscriptionSettings: vi.fn(),
}));
vi.mock('$lib/api/transcriptionSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/transcriptionSettings')>();
  return { ...actual, ...api };
});

import SpeakerDetectionSettings from './SpeakerDetectionSettings.svelte';
import { resetCapabilities } from '$stores/capabilities';

const SETTINGS = {
  min_speakers: 1,
  max_speakers: 20,
  speaker_prompt_behavior: 'always_prompt',
  garbage_cleanup_enabled: true,
  garbage_cleanup_threshold: 50,
  source_language: 'auto',
  translate_to_english: false,
  llm_output_language: 'en',
  vad_threshold: 0.5,
  vad_min_silence_ms: 2000,
  vad_min_speech_ms: 250,
  vad_speech_pad_ms: 400,
  hallucination_silence_threshold: null,
  repetition_penalty: 1.0,
  diarization_source: 'provider',
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS });
  api.getTranscriptionSystemDefaults.mockResolvedValue({
    min_speakers: 1,
    max_speakers: 20,
    garbage_cleanup_threshold: 50,
    diarization_source_default: 'provider',
    available_source_languages: { auto: 'Auto-detect', en: 'English' },
    available_llm_output_languages: { en: 'English', de: 'German' },
    common_languages: ['auto', 'en'],
  });
  api.updateTranscriptionSettings.mockImplementation(async (u) => ({ ...SETTINGS, ...u }));
});

afterEach(() => resetCapabilities());

async function renderLoaded() {
  const result = render(SpeakerDetectionSettings);
  await waitFor(() => expect(result.container.querySelector('#speaker-behavior')).not.toBeNull());
  return result;
}

async function changeBehavior(container: HTMLElement, value: string) {
  const select = container.querySelector('#speaker-behavior') as HTMLSelectElement;
  await fireEvent.change(select, { target: { value } });
}

async function clickSave(container: HTMLElement) {
  await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
  await waitFor(() => expect(api.updateTranscriptionSettings).toHaveBeenCalledOnce());
  return api.updateTranscriptionSettings.mock.calls[0][0] as Record<string, unknown>;
}

describe('SpeakerDetectionSettings', () => {
  it('saves exactly the four speaker fields and nothing from other groups', async () => {
    const { container } = await renderLoaded();
    await changeBehavior(container, 'use_defaults');
    const payload = await clickSave(container);
    expect(Object.keys(payload).sort()).toEqual([
      'diarization_source',
      'max_speakers',
      'min_speakers',
      'speaker_prompt_behavior',
    ]);
    expect(payload.speaker_prompt_behavior).toBe('use_defaults');
  });

  it('resets only the speakers group and re-applies its defaults', async () => {
    api.resetTranscriptionSettings.mockResolvedValue({
      message: 'ok',
      default_settings: { ...SETTINGS, speaker_prompt_behavior: 'always_prompt' },
    });
    api.getTranscriptionSettings.mockResolvedValue({
      ...SETTINGS,
      speaker_prompt_behavior: 'use_defaults',
    });
    const { container } = await renderLoaded();
    await fireEvent.click(container.querySelector('.btn-secondary') as HTMLButtonElement);
    await waitFor(() => expect(api.resetTranscriptionSettings).toHaveBeenCalledWith('speakers'));
    await waitFor(() =>
      expect((container.querySelector('#speaker-behavior') as HTMLSelectElement).value).toBe(
        'always_prompt'
      )
    );
  });

  it('keeps the detection source after a reset whose response omits it', async () => {
    const { diarization_source: _omitted, ...withoutSource } = SETTINGS;
    api.resetTranscriptionSettings.mockResolvedValue({
      message: 'ok',
      default_settings: withoutSource,
    });
    api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS, diarization_source: 'off' });
    const { container } = await renderLoaded();
    await fireEvent.click(container.querySelector('.btn-secondary') as HTMLButtonElement);
    await waitFor(() =>
      expect((container.querySelector('#diarization-source') as HTMLSelectElement).value).toBe(
        'provider'
      )
    );
  });

  it('renders the speaker-count options from translation keys, not English literals', async () => {
    const { container } = await renderLoaded();
    const labels = Array.from(container.querySelectorAll('#speaker-behavior option')).map(
      (o) => o.textContent?.trim()
    );
    expect(labels).toEqual([
      'settings.speakerIdentification.countMode.alwaysPrompt',
      'settings.speakerIdentification.countMode.useDefaults',
      'settings.speakerIdentification.countMode.useCustom',
    ]);
  });

  it('shows the min and max inputs only for a custom range and validates them', async () => {
    const { container } = await renderLoaded();
    expect(container.querySelector('#min-speakers')).toBeNull();
    await changeBehavior(container, 'use_custom');
    const min = container.querySelector('#min-speakers') as HTMLInputElement;
    const max = container.querySelector('#max-speakers') as HTMLInputElement;
    expect(max).not.toBeNull();
    await fireEvent.input(min, { target: { value: '30' } });
    await fireEvent.input(max, { target: { value: '5' } });
    expect(container.querySelector('.validation-error')?.textContent).toContain(
      'settings.speakerIdentification.validationMinMax'
    );
    expect((container.querySelector('.btn-primary') as HTMLButtonElement).disabled).toBe(true);
  });
});
