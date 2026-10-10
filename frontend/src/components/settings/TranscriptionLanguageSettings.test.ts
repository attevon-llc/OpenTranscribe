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

import TranscriptionLanguageSettings from './TranscriptionLanguageSettings.svelte';
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
  const result = render(TranscriptionLanguageSettings);
  await waitFor(() =>
    expect(result.container.querySelector('#llm-output-language')).not.toBeNull()
  );
  return result;
}

describe('TranscriptionLanguageSettings', () => {
  it('saves only the three language fields', async () => {
    const { container } = await renderLoaded();
    await fireEvent.change(container.querySelector('#llm-output-language') as HTMLSelectElement, {
      target: { value: 'de' },
    });
    await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
    await waitFor(() => expect(api.updateTranscriptionSettings).toHaveBeenCalledOnce());
    expect(api.updateTranscriptionSettings).toHaveBeenCalledWith({
      source_language: 'auto',
      translate_to_english: false,
      llm_output_language: 'de',
    });
  });

  it('resets only the language group and applies its defaults', async () => {
    api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS, llm_output_language: 'de' });
    api.resetTranscriptionSettings.mockResolvedValue({ message: 'ok', default_settings: SETTINGS });
    const { container } = await renderLoaded();
    await fireEvent.click(container.querySelector('.btn-secondary') as HTMLButtonElement);
    await waitFor(() => expect(api.resetTranscriptionSettings).toHaveBeenCalledWith('language'));
    await waitFor(() =>
      expect((container.querySelector('#llm-output-language') as HTMLSelectElement).value).toBe(
        'en'
      )
    );
  });

  it('keeps the save button disabled until a field changes', async () => {
    const { container } = await renderLoaded();
    const save = container.querySelector('.btn-primary') as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await fireEvent.change(container.querySelector('#source-language') as HTMLSelectElement, {
      target: { value: 'en' },
    });
    expect(save.disabled).toBe(false);
  });

  it('renders the AI-language select in its own card', async () => {
    const { container } = await renderLoaded();
    const card = (container.querySelector('#llm-output-language') as HTMLElement).closest(
      '.settings-section'
    );
    expect(card?.querySelector('#source-language')).toBeNull();
    expect(card?.querySelector('.section-title')?.textContent).toContain(
      'settings.transcription.aiLanguageHeading'
    );
  });
});
