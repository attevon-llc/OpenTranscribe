import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});
const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

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
const pipelineApi = vi.hoisted(() => ({ getPipelineView: vi.fn() }));
vi.mock('$lib/api/pipeline', () => pipelineApi);
const asrApi = vi.hoisted(() => ({
  getSettings: vi.fn(),
  getProviders: vi.fn(),
  getProviderDisplayName: (p: string) => p,
}));
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asrApi }));
vi.mock('$lib/api/pyannoteCredential', () => ({
  getPyannoteCredential: vi.fn().mockResolvedValue({
    configured: true,
    locked: false,
    test_status: null,
    test_message: null,
    last_tested: null,
    updated_at: null,
  }),
  savePyannoteCredential: vi.fn(),
  deletePyannoteCredential: vi.fn(),
  testPyannoteCredential: vi.fn(),
}));

import SpeakerDetectionSettings from './SpeakerDetectionSettings.svelte';
import { capabilities, resetCapabilities } from '$stores/capabilities';
import { fullLocalView, liteCloudView } from '$lib/settings/pipelineViewFixtures';

const SETTINGS = {
  min_speakers: 1,
  max_speakers: 20,
  speaker_prompt_behavior: 'always_prompt',
  diarization_source: 'provider',
  diarization_asr_config_uuid: null,
};

const PROVIDERS = [
  {
    id: 'deepgram',
    display_name: 'Deepgram',
    supports_diarization: true,
    speaker_count_hints: [],
    timestamp_granularity: 'word',
    models: [{ id: 'nova-3', display_name: 'Nova 3' }],
  },
  {
    id: 'openai',
    display_name: 'OpenAI',
    supports_diarization: false,
    speaker_count_hints: [],
    timestamp_granularity: 'none',
    models: [{ id: 'gpt-4o-transcribe', display_name: 'gpt-4o-transcribe' }],
  },
];
const CONFIG = (uuid: string, provider: string, model: string) => ({
  uuid,
  name: `${provider} config`,
  provider,
  model_name: model,
});

beforeEach(() => {
  vi.clearAllMocks();
  api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS });
  api.getTranscriptionSystemDefaults.mockResolvedValue({
    min_speakers: 1,
    max_speakers: 20,
    diarization_source_default: 'provider',
  });
  api.updateTranscriptionSettings.mockImplementation(async (u) => ({ ...SETTINGS, ...u }));
  pipelineApi.getPipelineView.mockResolvedValue(fullLocalView());
  asrApi.getSettings.mockResolvedValue({
    configurations: [
      CONFIG('dg', 'deepgram', 'nova-3'),
      CONFIG('oa', 'openai', 'gpt-4o-transcribe'),
    ],
    shared_configurations: [],
    active_configuration_uuid: undefined,
    total: 2,
  });
  asrApi.getProviders.mockResolvedValue({ providers: PROVIDERS });
});
afterEach(() => resetCapabilities());

async function renderLoaded() {
  const result = render(SpeakerDetectionSettings);
  await waitFor(() => expect(result.container.querySelector('#speaker-behavior')).not.toBeNull());
  // The pipeline view and the saved configs load beside the settings.
  await waitFor(() => expect(pipelineApi.getPipelineView).toHaveBeenCalled());
  return result;
}
const sourceSelect = (c: HTMLElement) =>
  c.querySelector('#diarization-source') as HTMLSelectElement;
const choose = (c: HTMLElement, value: string) =>
  fireEvent.change(sourceSelect(c), { target: { value } });
const save = async (c: HTMLElement) => {
  await fireEvent.click(c.querySelector('.btn-primary') as HTMLButtonElement);
  await waitFor(() => expect(api.updateTranscriptionSettings).toHaveBeenCalledOnce());
  return api.updateTranscriptionSettings.mock.calls[0][0] as Record<string, unknown>;
};
const conflict = (code: string) => ({
  response: { status: 409, data: { detail: { code, message: 'fixed sentence' } } },
});

describe('asr_config as the speaker source', () => {
  it('offers the saved configs whose model returns speakers, and saves the pick', async () => {
    const { container } = await renderLoaded();
    await waitFor(() => expect(asrApi.getProviders).toHaveBeenCalled());
    await choose(container, 'asr_config');
    const picker = await waitFor(() => {
      const el = container.querySelector('#diarization-asr-config') as HTMLSelectElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(Array.from(picker.options).map((o) => o.value)).toEqual(['dg']);

    const payload = await save(container);
    expect(payload.diarization_source).toBe('asr_config');
    expect(payload.diarization_asr_config_uuid).toBe('dg');
  });

  it('does not send the config uuid for any other source', async () => {
    const { container } = await renderLoaded();
    await choose(container, 'pyannote');
    const payload = await save(container);
    expect(payload).not.toHaveProperty('diarization_asr_config_uuid');
  });

  it('loads a stored asr_config choice and treats a changed pick as unsaved', async () => {
    api.getTranscriptionSettings.mockResolvedValue({
      ...SETTINGS,
      diarization_source: 'asr_config',
      diarization_asr_config_uuid: 'dg',
    });
    asrApi.getSettings.mockResolvedValue({
      configurations: [CONFIG('dg', 'deepgram', 'nova-3'), CONFIG('dg2', 'deepgram', 'nova-3')],
      shared_configurations: [],
      active_configuration_uuid: undefined,
      total: 2,
    });
    const { container } = await renderLoaded();
    const picker = await waitFor(() => {
      const el = container.querySelector('#diarization-asr-config') as HTMLSelectElement;
      expect(el.options.length).toBe(2);
      return el;
    });
    expect(picker.value).toBe('dg');
    expect((container.querySelector('.btn-primary') as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.change(picker, { target: { value: 'dg2' } });
    expect((container.querySelector('.btn-primary') as HTMLButtonElement).disabled).toBe(false);
  });

  it('never loads the saved configs, or offers the option, without asr.user_providers', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'asr.user_providers': false },
      audience: {},
      maxUploadBytes: undefined,
    });
    const { container } = await renderLoaded();
    expect(asrApi.getSettings).not.toHaveBeenCalled();
    expect(Array.from(sourceSelect(container).options).map((o) => o.value)).not.toContain(
      'asr_config'
    );
  });
});

describe('a refused save reads the code, not the status', () => {
  it.each([
    ['pyannote_key_missing', 'settings.speakerIdentification.pyannoteKey.requiredToSelect'],
    ['asr_no_timing_for_speakers', 'settings.pipeline.issue.asr_no_timing_for_speakers'],
    ['diarizer_config_missing', 'settings.pipeline.issue.diarizer_config_missing'],
    ['diarizer_config_no_speakers', 'settings.pipeline.issue.diarizer_config_no_speakers'],
    ['local_diarization_unavailable', 'settings.pipeline.issue.local_diarization_unavailable'],
    [
      'automatic_needs_speaker_provider',
      'settings.pipeline.issue.automatic_needs_speaker_provider',
    ],
  ])('%s shows its own sentence', async (code, expected) => {
    api.updateTranscriptionSettings.mockRejectedValue(conflict(code));
    const { container } = await renderLoaded();
    await choose(container, 'pyannote');
    await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expected));
  });

  it('never tells the user to save a key for a refusal that is not about the key', async () => {
    api.updateTranscriptionSettings.mockRejectedValue(conflict('asr_no_timing_for_speakers'));
    const { container } = await renderLoaded();
    await choose(container, 'pyannote');
    await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
    await waitFor(() => expect(toast.error).toHaveBeenCalledOnce());
    expect(toast.error).not.toHaveBeenCalledWith(
      'settings.speakerIdentification.pyannoteKey.requiredToSelect'
    );
  });

  it('falls back to the generic message for a code it does not know', async () => {
    api.updateTranscriptionSettings.mockRejectedValue(conflict('unheard_of'));
    const { container } = await renderLoaded();
    await choose(container, 'pyannote');
    await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('settings.speakerIdentification.saveFailed')
    );
  });
});

describe('lite deployment', () => {
  it('has no local option in the select', async () => {
    pipelineApi.getPipelineView.mockResolvedValue(liteCloudView());
    const { container } = await renderLoaded();
    await waitFor(() =>
      expect(Array.from(sourceSelect(container).options).map((o) => o.value)).not.toContain('local')
    );
  });
});

describe('the pipeline view stays in step with the form', () => {
  it('reloads after a successful save', async () => {
    const { container } = await renderLoaded();
    pipelineApi.getPipelineView.mockClear();
    await choose(container, 'off');
    await save(container);
    await waitFor(() => expect(pipelineApi.getPipelineView).toHaveBeenCalledOnce());
  });

  it('still renders the form when the pipeline view cannot be loaded', async () => {
    pipelineApi.getPipelineView.mockRejectedValue(new Error('down'));
    const { container } = await renderLoaded();
    expect(sourceSelect(container)).not.toBeNull();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('says the engine ignores the speaker range when the plan reports it', async () => {
    const { container } = await renderLoaded();
    await fireEvent.change(container.querySelector('#speaker-behavior') as HTMLSelectElement, {
      target: { value: 'use_custom' },
    });
    await waitFor(() =>
      expect(container.querySelector('[data-testid="hints-ignored-note"]')?.textContent).toContain(
        'settings.pipeline.issue.speaker_hints_ignored'
      )
    );
  });

  it('says nothing about the range when the engine uses it', async () => {
    const view = fullLocalView({ issues: [] });
    view.steps[1] = { ...view.steps[1], speaker_hints: ['min', 'max'] };
    pipelineApi.getPipelineView.mockResolvedValue(view);
    const { container } = await renderLoaded();
    await fireEvent.change(container.querySelector('#speaker-behavior') as HTMLSelectElement, {
      target: { value: 'use_custom' },
    });
    expect(container.querySelector('#min-speakers')).not.toBeNull();
    expect(container.querySelector('[data-testid="hints-ignored-note"]')).toBeNull();
  });
});
