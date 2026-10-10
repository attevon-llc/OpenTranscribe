import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';

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
    getProviders: vi.fn().mockResolvedValue({ providers: [] }),
    getSettings: vi.fn().mockResolvedValue({
      configurations: [],
      shared_configurations: [],
      active_configuration_id: undefined,
      total: 0,
    }),
    getActiveLocalModel: vi.fn().mockResolvedValue({
      active_model: 'large-v3-turbo',
      source: 'environment',
      available_models: [],
      model_info: null,
    }),
    getProviderDisplayName: (p: string) => p,
  },
  CustomVocabularyApi: { getVocabulary: vi.fn().mockResolvedValue([]) },
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

import TranscriptionSection from './TranscriptionSection.svelte';
import { settingsModalStore } from '$stores/settingsModalStore';
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
  resetCapabilities();
  settingsModalStore.reset();
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
});

const tab = (c: HTMLElement, id: string) => c.querySelector(`#tab-${id}`) as HTMLElement | null;
const panel = (c: HTMLElement, id: string) => c.querySelector(`#tabpanel-${id}`) as HTMLElement;

describe('TranscriptionSection', () => {
  it('renders the four tabs in order, language first and selected', () => {
    const { container } = render(TranscriptionSection);
    const ids = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.id);
    expect(ids).toEqual([
      'tab-tx-language',
      'tab-tx-provider',
      'tab-tx-vocabulary',
      'tab-tx-accuracy',
    ]);
    expect(tab(container, 'tx-language')?.getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('[role="tablist"]')?.getAttribute('aria-label')).toBe(
      'settings.transcription.tabs.ariaLabel'
    );
  });

  it('mounts a panel on first visit and keeps it mounted, hidden, after switching away', async () => {
    const { container } = render(TranscriptionSection);
    expect(
      panel(container, 'tx-accuracy').querySelector('.transcription-accuracy-settings')
    ).toBeNull();

    await fireEvent.click(tab(container, 'tx-accuracy') as HTMLElement);
    await waitFor(() =>
      expect(
        panel(container, 'tx-accuracy').querySelector('.transcription-accuracy-settings')
      ).not.toBeNull()
    );
    await fireEvent.click(tab(container, 'tx-language') as HTMLElement);

    expect(panel(container, 'tx-accuracy').hidden).toBe(true);
    expect(
      panel(container, 'tx-accuracy').querySelector('.transcription-accuracy-settings')
    ).not.toBeNull();
    expect(panel(container, 'tx-language').hidden).toBe(false);
  });

  it('opens the requested tab', () => {
    const { container } = render(TranscriptionSection, { props: { initialTab: 'tx-vocabulary' } });
    expect(tab(container, 'tx-vocabulary')?.getAttribute('aria-selected')).toBe('true');
  });

  it('drops the provider tab when the ASR capability is off and falls back from it', () => {
    const { container } = render(TranscriptionSection, {
      props: { asrCap: false, initialTab: 'tx-provider' },
    });
    expect(tab(container, 'tx-provider')).toBeNull();
    expect(tab(container, 'tx-language')?.getAttribute('aria-selected')).toBe('true');
  });

  it('badges the tab with unsaved edits and flags the whole section dirty', async () => {
    const { container } = render(TranscriptionSection);
    await waitFor(() => expect(container.querySelector('#source-language')).not.toBeNull());
    expect(get(settingsModalStore).dirtyState.transcription).toBeFalsy();

    await fireEvent.change(container.querySelector('#source-language') as HTMLSelectElement, {
      target: { value: 'en' },
    });

    await waitFor(() => expect(get(settingsModalStore).dirtyState.transcription).toBe(true));
    expect(tab(container, 'tx-language')?.querySelector('.tab-badge')?.textContent).toBe('●');
    expect(tab(container, 'tx-accuracy')?.querySelector('.tab-badge')).toBeNull();
  });

  it('keeps the edit when the user switches to another tab and back', async () => {
    const { container } = render(TranscriptionSection);
    await waitFor(() => expect(container.querySelector('#source-language')).not.toBeNull());
    await fireEvent.change(container.querySelector('#source-language') as HTMLSelectElement, {
      target: { value: 'en' },
    });
    await fireEvent.click(tab(container, 'tx-vocabulary') as HTMLElement);
    await fireEvent.click(tab(container, 'tx-language') as HTMLElement);

    expect((container.querySelector('#source-language') as HTMLSelectElement).value).toBe('en');
  });
});
