import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const asrApi = vi.hoisted(() => ({
  createConfig: vi.fn(),
  getProviderDisplayName: (p: string) => p,
  formatPricePerHour: () => '',
}));
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asrApi }));
const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import ASRConfigModal from './ASRConfigModal.svelte';
import type { ASRProviderInfo } from '$lib/api/asrSettings';
import { issue } from '$lib/settings/pipelineViewFixtures';

const PROVIDERS = [
  {
    id: 'local',
    provider: 'local',
    display_name: 'Local',
    requires_api_key: false,
    supports_diarization: true,
    speaker_count_hints: [],
    timestamp_granularity: 'word',
    status: 'tested',
    sdk_available: true,
    models: [{ id: 'large-v3', display_name: 'large-v3' }],
  },
  {
    id: 'openai',
    provider: 'openai',
    display_name: 'OpenAI',
    requires_api_key: true,
    supports_diarization: false,
    speaker_count_hints: [],
    timestamp_granularity: 'none',
    status: 'tested',
    sdk_available: true,
    models: [{ id: 'gpt-4o-transcribe', display_name: 'gpt-4o-transcribe' }],
  },
] as unknown as ASRProviderInfo[];

beforeEach(() => vi.clearAllMocks());

async function createOpenAI(props: Record<string, unknown>) {
  render(ASRConfigModal, { props: { show: true, providers: PROVIDERS, ...props } });
  const provider = await waitFor(() => {
    const el = document.querySelector('#asr-provider') as HTMLSelectElement;
    expect(el).not.toBeNull();
    return el;
  });
  await fireEvent.input(document.querySelector('#asr-name') as HTMLInputElement, {
    target: { value: 'My OpenAI' },
  });
  await fireEvent.change(provider, { target: { value: 'openai' } });
  await fireEvent.change(
    await waitFor(() => document.querySelector('#asr-model') as HTMLSelectElement),
    { target: { value: 'gpt-4o-transcribe' } }
  );
  await fireEvent.input(
    await waitFor(() => document.querySelector('#asr-apikey') as HTMLInputElement),
    {
      target: { value: 'sk-test' },
    }
  );
  await fireEvent.click(document.querySelector('.btn-save') as HTMLButtonElement);
}

describe('creating a config', () => {
  it('tells the user a first config was saved but not made active, and why', async () => {
    asrApi.createConfig.mockResolvedValue({
      name: 'My OpenAI',
      auto_activated: false,
      pipeline_issues: [issue('asr_no_timing_for_speakers', 'error')],
    });
    await createOpenAI({ firstConfig: true });
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith('settings.asrProvider.notAutoActivated', 8000)
    );
    expect(toast.error).toHaveBeenCalledWith(
      'settings.pipeline.issue.asr_no_timing_for_speakers',
      8000
    );
  });

  it('says nothing extra when the first config was made active', async () => {
    asrApi.createConfig.mockResolvedValue({
      name: 'My OpenAI',
      auto_activated: true,
      pipeline_issues: [],
    });
    await createOpenAI({ firstConfig: true });
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(toast.warning).not.toHaveBeenCalled();
  });

  it('does not nag when it is not the first config', async () => {
    asrApi.createConfig.mockResolvedValue({
      name: 'My OpenAI',
      auto_activated: false,
      pipeline_issues: [],
    });
    await createOpenAI({ firstConfig: false });
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(toast.warning).not.toHaveBeenCalled();
  });
});

describe('provider picker on lite', () => {
  const options = () =>
    Array.from((document.querySelector('#asr-provider') as HTMLSelectElement).options).map(
      (o) => o.value
    );

  it('does not offer the local provider', async () => {
    render(ASRConfigModal, { props: { show: true, providers: PROVIDERS, deploymentMode: 'lite' } });
    await waitFor(() => expect(document.querySelector('#asr-provider')).not.toBeNull());
    expect(options()).not.toContain('local');
    expect(options()).toContain('openai');
  });

  it('offers it on a full deployment', async () => {
    render(ASRConfigModal, { props: { show: true, providers: PROVIDERS } });
    await waitFor(() => expect(document.querySelector('#asr-provider')).not.toBeNull());
    expect(options()).toContain('local');
  });
});
