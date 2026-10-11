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
const key = vi.hoisted(() => ({
  getPyannoteCredential: vi.fn(),
  savePyannoteCredential: vi.fn(),
  deletePyannoteCredential: vi.fn(),
  testPyannoteCredential: vi.fn(),
}));
vi.mock('$lib/api/pyannoteCredential', () => key);

import SpeakerDetectionSettings from './SpeakerDetectionSettings.svelte';
import { fullLocalView } from '$lib/settings/pipelineViewFixtures';
import { capabilities, resetCapabilities } from '$stores/capabilities';

const SETTINGS = {
  min_speakers: 1,
  max_speakers: 20,
  speaker_prompt_behavior: 'always_prompt',
  diarization_source: 'provider',
};
const STATUS = {
  configured: false,
  locked: false,
  test_status: null,
  test_message: null,
  last_tested: null,
  updated_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  pipelineApi.getPipelineView.mockResolvedValue(fullLocalView());
  asrApi.getSettings.mockResolvedValue({
    configurations: [],
    shared_configurations: [],
    active_configuration_uuid: undefined,
    total: 0,
  });
  asrApi.getProviders.mockResolvedValue({ providers: [] });
  api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS });
  api.getTranscriptionSystemDefaults.mockResolvedValue({
    min_speakers: 1,
    max_speakers: 20,
    diarization_source_default: 'provider',
  });
  key.getPyannoteCredential.mockResolvedValue({ ...STATUS });
});
afterEach(() => resetCapabilities());

async function renderLoaded() {
  const result = render(SpeakerDetectionSettings);
  await waitFor(() => expect(result.container.querySelector('#speaker-behavior')).not.toBeNull());
  return result;
}
const form = (c: HTMLElement) => c.querySelector('[data-testid="pyannote-credential-form"]');
const choosePyannote = (c: HTMLElement) =>
  fireEvent.change(c.querySelector('#diarization-source') as HTMLSelectElement, {
    target: { value: 'pyannote' },
  });

describe('pyannote.ai key form in Speaker Detection', () => {
  it('is hidden until the pyannote.ai cloud service is the chosen provider', async () => {
    const { container } = await renderLoaded();
    expect(form(container)).toBeNull();
    await choosePyannote(container);
    expect(form(container)).not.toBeNull();
    await fireEvent.change(container.querySelector('#diarization-source') as HTMLSelectElement, {
      target: { value: 'local' },
    });
    expect(form(container)).toBeNull();
  });

  it('renders under the source select when users may bring their own provider keys', async () => {
    const { container } = await renderLoaded();
    await choosePyannote(container);
    const select = container.querySelector('#diarization-source') as HTMLElement;
    expect(form(container)).not.toBeNull();
    // Same card, after the select
    expect(
      select.compareDocumentPosition(form(container) as Node) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it('is absent, and the credential is never requested, when asr.user_providers is off', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'asr.user_providers': false },
      audience: {},
      maxUploadBytes: undefined,
    });
    const { container } = await renderLoaded();
    expect(form(container)).toBeNull();
    expect(key.getPyannoteCredential).not.toHaveBeenCalled();
  });

  it('is absent when the deployment locks the detection source, which makes the select read-only', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'transcription.diarization_source': false },
      audience: {},
      maxUploadBytes: undefined,
    });
    const { container } = await renderLoaded();
    expect((container.querySelector('#diarization-source') as HTMLSelectElement).disabled).toBe(
      true
    );
    expect(form(container)).toBeNull();
  });

  it('keeps the form from hijacking the Save and Reset buttons of the settings form', async () => {
    const { container } = await renderLoaded();
    await choosePyannote(container);
    const primary = container.querySelectorAll('.btn-primary');
    expect(primary).toHaveLength(1);
    expect(form(container)?.querySelector('.btn-primary, .btn-secondary')).toBeNull();
  });

  it('explains a pyannote_key_missing 409 as "save a key first" instead of the generic failure', async () => {
    api.updateTranscriptionSettings.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { code: 'pyannote_key_missing', message: 'fixed sentence' } },
      },
    });
    const { container } = await renderLoaded();
    await fireEvent.change(container.querySelector('#diarization-source') as HTMLSelectElement, {
      target: { value: 'pyannote' },
    });
    await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'settings.speakerIdentification.pyannoteKey.requiredToSelect'
      )
    );
  });

  it('keeps the generic message for a 409 that carries no code, whatever the source', async () => {
    api.updateTranscriptionSettings.mockRejectedValue({ response: { status: 409 } });
    const { container } = await renderLoaded();
    await fireEvent.change(container.querySelector('#speaker-behavior') as HTMLSelectElement, {
      target: { value: 'use_defaults' },
    });
    await fireEvent.click(container.querySelector('.btn-primary') as HTMLButtonElement);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('settings.speakerIdentification.saveFailed')
    );
  });

  it('reloads the source select when removing the key reverted it, keeping other edits', async () => {
    api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS, diarization_source: 'pyannote' });
    key.getPyannoteCredential.mockResolvedValue({ ...STATUS, configured: true });
    key.deletePyannoteCredential.mockResolvedValue({
      deleted: true,
      diarization_source: 'provider',
      source_reverted: true,
    });
    const { container } = await renderLoaded();
    const select = container.querySelector('#diarization-source') as HTMLSelectElement;
    expect(select.value).toBe('pyannote');
    await fireEvent.change(container.querySelector('#speaker-behavior') as HTMLSelectElement, {
      target: { value: 'use_defaults' },
    });

    api.getTranscriptionSettings.mockResolvedValue({ ...SETTINGS, diarization_source: 'provider' });
    const remove = Array.from(form(container)!.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('pyannoteKey.delete')
    ) as HTMLButtonElement;
    await fireEvent.click(remove);
    await fireEvent.click(
      await waitFor(() => {
        const el = document.querySelector('.modal-delete-button');
        expect(el).not.toBeNull();
        return el as HTMLElement;
      })
    );

    await waitFor(() => expect(select.value).toBe('provider'));
    expect((container.querySelector('#speaker-behavior') as HTMLSelectElement).value).toBe(
      'use_defaults'
    );
  });
});
