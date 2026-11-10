import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const asrApi = vi.hoisted(() => ({
  createConfig: vi.fn(),
  getProviderDisplayName: (p: string) => `name:${p}`,
  formatPricePerHour: () => '',
}));
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asrApi }));
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyWithVarsLocale());

import ASRConfigModal from './ASRConfigModal.svelte';
import type { ASRProviderInfo } from '$lib/api/asrSettings';

const provider = (id: string) => ({
  id,
  provider: id,
  display_name: id,
  requires_api_key: true,
  supports_diarization: false,
  speaker_count_hints: [],
  timestamp_granularity: 'none',
  status: 'tested',
  sdk_available: true,
  models: [{ id: `${id}-model`, display_name: `${id}-model` }],
});
const PROVIDERS = [provider('deepgram'), provider('assemblyai')] as unknown as ASRProviderInfo[];

beforeEach(() => vi.clearAllMocks());

describe('ASRConfigModal presets (adding shared credentials from the policy panel)', () => {
  it('opens with the provider chosen and sharing on', async () => {
    render(ASRConfigModal, {
      props: { show: true, providers: PROVIDERS, presetProvider: 'assemblyai', presetShared: true },
    });
    const select = await waitFor(() => {
      const el = document.querySelector('#asr-provider') as HTMLSelectElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(select.value).toBe('assemblyai');
    expect((document.querySelector('#asr-name') as HTMLInputElement).value).toBe('name:assemblyai');
    expect((document.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(true);
  });

  it('creates the shared config with is_shared true and the preset provider', async () => {
    asrApi.createConfig.mockResolvedValue({ name: 'name:assemblyai', auto_activated: false });
    render(ASRConfigModal, {
      props: { show: true, providers: PROVIDERS, presetProvider: 'assemblyai', presetShared: true },
    });
    await waitFor(() => expect(document.querySelector('#asr-provider')).not.toBeNull());
    await fireEvent.change(
      await waitFor(() => document.querySelector('#asr-model') as HTMLSelectElement),
      { target: { value: 'assemblyai-model' } }
    );
    await fireEvent.input(
      await waitFor(() => document.querySelector('#asr-apikey') as HTMLInputElement),
      { target: { value: 'k' } }
    );
    await fireEvent.click(document.querySelector('.btn-save') as HTMLButtonElement);
    await waitFor(() =>
      expect(asrApi.createConfig).toHaveBeenCalledWith(
        expect.objectContaining({ provider: 'assemblyai', is_shared: true })
      )
    );
  });

  it('leaves the form empty and private without a preset (control)', async () => {
    render(ASRConfigModal, { props: { show: true, providers: PROVIDERS } });
    const select = await waitFor(
      () => document.querySelector('#asr-provider') as HTMLSelectElement
    );
    expect(select.value).toBe('');
    expect((document.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(false);
  });
});

describe('ASRConfigModal share switch', () => {
  it('flips when its text is clicked', async () => {
    render(ASRConfigModal, { props: { show: true, providers: PROVIDERS } });
    const sw = (await waitFor(() => {
      const el = document.querySelector('[role="switch"]') as HTMLInputElement;
      expect(el).not.toBeNull();
      return el;
    })) as HTMLInputElement;
    expect(sw.checked).toBe(false);
    await fireEvent.click(document.querySelector('label[for="asr-share-globally-label"]')!);
    await waitFor(() => expect(sw.checked).toBe(true));
  });
});
