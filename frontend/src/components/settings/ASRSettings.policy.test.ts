import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const asrApi = vi.hoisted(() => ({
  getProviders: vi.fn(),
  getSettings: vi.fn(),
  getActiveLocalModel: vi.fn(),
  getStatus: vi.fn(),
  getProviderDisplayName: (p: string) => `name:${p}`,
  formatPricePerHour: () => '',
}));
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asrApi }));
const speakerApi = vi.hoisted(() => ({ getTranscriptionSettings: vi.fn() }));
vi.mock('$lib/api/transcriptionSettings', () => speakerApi);
const policyApi = vi.hoisted(() => ({ getProviderPolicy: vi.fn(), updateProviderPolicy: vi.fn() }));
vi.mock('$lib/api/asrProviderPolicy', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api/asrProviderPolicy')>()),
  getProviderPolicy: policyApi.getProviderPolicy,
  updateProviderPolicy: policyApi.updateProviderPolicy,
}));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import ASRSettings from './ASRSettings.svelte';

const provider = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  provider: id,
  display_name: id,
  name: id,
  requires_api_key: id !== 'local',
  supports_diarization: false,
  speaker_count_hints: [],
  timestamp_granularity: 'none',
  status: 'tested',
  models: [{ id: `${id}-m`, name: `${id}-m`, display_name: `${id}-m` }],
  ...extra,
});
const config = (uuid: string, providerId: string, extra: Record<string, unknown> = {}) => ({
  id: 1,
  uuid,
  user_id: 1,
  name: `cfg-${uuid}`,
  provider: providerId,
  model_name: `${providerId}-m`,
  is_active: false,
  has_api_key: true,
  is_shared: false,
  created_at: '',
  updated_at: '',
  ...extra,
});

function load(providers: unknown[], configurations: unknown[] = [], shared: unknown[] = []) {
  asrApi.getProviders.mockResolvedValue({ providers });
  asrApi.getSettings.mockResolvedValue({
    configurations,
    shared_configurations: shared,
    active_configuration_id: null,
    total: configurations.length,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  asrApi.getActiveLocalModel.mockResolvedValue(null);
  asrApi.getStatus.mockResolvedValue({ deployment_mode: 'full' });
  speakerApi.getTranscriptionSettings.mockResolvedValue(null);
  policyApi.getProviderPolicy.mockResolvedValue({
    providers: [
      {
        id: 'assemblyai',
        display_name: 'AssemblyAI',
        allowed_for_users: true,
        shared_credentials: [],
      },
    ],
    users_may_add_own_keys: true,
  });
});

describe('cloud provider policy placement', () => {
  it('a super administrator gets the editable panel', async () => {
    load([provider('local'), provider('assemblyai')]);
    render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: true } });
    await screen.findByText('AssemblyAI');
    expect(policyApi.getProviderPolicy).toHaveBeenCalledTimes(1);
  });

  it('an administrator gets the panel locked and nothing is requested', async () => {
    load([provider('local'), provider('assemblyai')]);
    render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: false } });
    await screen.findByTestId('asr-policy-locked');
    expect(policyApi.getProviderPolicy).not.toHaveBeenCalled();
  });

  it('a plain user never sees the panel and nothing is requested', async () => {
    load([provider('local'), provider('assemblyai')]);
    const { container } = render(ASRSettings, { props: { isAdmin: false } });
    await waitFor(() => expect(asrApi.getProviders).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('.status-bar')).not.toBeNull());
    expect(container.querySelector('[data-testid="asr-policy"]')).toBeNull();
    expect(policyApi.getProviderPolicy).not.toHaveBeenCalled();
  });
});

describe('add shared credentials', () => {
  it('opens the config editor on that provider with sharing on', async () => {
    load([provider('local'), provider('assemblyai')]);
    render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: true } });
    await fireEvent.click(
      await screen.findByRole('button', {
        name: 'settings.asrPolicy.addSharedFor{"name":"AssemblyAI"}',
      })
    );
    const select = await waitFor(() => {
      const el = document.querySelector('#asr-provider') as HTMLSelectElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(select.value).toBe('assemblyai');
    expect((document.querySelector('.toggle-input') as HTMLInputElement).checked).toBe(true);
  });

  it('the ordinary Add button still opens an empty private editor afterwards', async () => {
    load([provider('local'), provider('assemblyai')], [config('a', 'assemblyai')]);
    const { container } = render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: true } });
    await fireEvent.click(
      await screen.findByRole('button', {
        name: 'settings.asrPolicy.addSharedFor{"name":"AssemblyAI"}',
      })
    );
    await waitFor(() => expect(document.querySelector('#asr-provider')).not.toBeNull());
    await fireEvent.click(screen.getByRole('button', { name: 'modal.closeDialog' }));
    await waitFor(() => expect(document.querySelector('#asr-provider')).toBeNull());

    await fireEvent.click(container.querySelector('.add-button') as HTMLElement);
    const select = await waitFor(
      () => document.querySelector('#asr-provider') as HTMLSelectElement
    );
    expect(select.value).toBe('');
    expect((document.querySelector('.toggle-input') as HTMLInputElement).checked).toBe(false);
  });
});

describe('what a plain user sees', () => {
  it('says so when the administrator enabled no cloud provider', async () => {
    load([provider('local')]);
    render(ASRSettings, { props: { isAdmin: false } });
    expect(await screen.findByTestId('asr-policy-none-enabled')).toBeTruthy();
  });

  it('does not say so while a cloud provider is enabled (control)', async () => {
    load([provider('local'), provider('assemblyai')]);
    const { container } = render(ASRSettings, { props: { isAdmin: false } });
    await waitFor(() => expect(container.querySelector('.status-bar')).not.toBeNull());
    expect(screen.queryByTestId('asr-policy-none-enabled')).toBeNull();
  });

  it('marks a saved config whose provider is no longer enabled and blocks activating it', async () => {
    load(
      [provider('local'), provider('assemblyai')],
      [config('ok', 'assemblyai'), config('gone', 'deepgram')]
    );
    const { container } = render(ASRSettings, { props: { isAdmin: false } });
    await waitFor(() => expect(container.querySelectorAll('.config-card').length).toBe(2));
    const cards = Array.from(container.querySelectorAll<HTMLElement>('.config-card'));
    const gone = cards.find((c) => c.textContent?.includes('cfg-gone'))!;
    const ok = cards.find((c) => c.textContent?.includes('cfg-ok'))!;
    expect(gone.textContent).toContain('settings.asrPolicy.notEnabled');
    expect((gone.querySelector('.btn-activate') as HTMLButtonElement).disabled).toBe(true);
    expect(ok.textContent).not.toContain('settings.asrPolicy.notEnabled');
    expect((ok.querySelector('.btn-activate') as HTMLButtonElement).disabled).toBe(false);
  });

  it('never marks anything for an administrator, who is not limited by the policy', async () => {
    load([provider('local'), provider('assemblyai')], [config('gone', 'deepgram')]);
    const { container } = render(ASRSettings, { props: { isAdmin: true, isSuperAdmin: false } });
    await waitFor(() => expect(container.querySelectorAll('.config-card').length).toBe(1));
    expect(container.textContent).not.toContain('settings.asrPolicy.notEnabled');
  });
});
