import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const asrApi = vi.hoisted(() => ({
  getProviders: vi.fn(),
  getSettings: vi.fn(),
  getActiveLocalModel: vi.fn(),
  getStatus: vi.fn(),
  setActive: vi.fn(),
  setEnabled: vi.fn(),
  getProviderDisplayName: (p: string) => p,
}));
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asrApi }));
const speakerApi = vi.hoisted(() => ({ getTranscriptionSettings: vi.fn() }));
vi.mock('$lib/api/transcriptionSettings', () => speakerApi);
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

import ASRSettings from './ASRSettings.svelte';

const PROVIDERS = [
  {
    id: 'deepgram',
    provider: 'deepgram',
    display_name: 'Deepgram',
    supports_diarization: true,
    speaker_count_hints: [],
    timestamp_granularity: 'word',
    status: 'tested',
    models: [{ id: 'nova-3', display_name: 'nova-3' }],
  },
];
const config = (uuid: string, isActive = true) => ({
  id: 1,
  uuid,
  user_id: 1,
  name: `cfg-${uuid}`,
  provider: 'deepgram',
  model_name: 'nova-3',
  is_active: isActive,
  has_api_key: true,
  is_shared: false,
  created_at: '',
  updated_at: '',
});

beforeEach(() => {
  vi.clearAllMocks();
  asrApi.getProviders.mockResolvedValue({ providers: PROVIDERS });
  asrApi.getSettings.mockResolvedValue({
    configurations: [config('a'), config('b', false)],
    shared_configurations: [],
    active_configuration_uuid: 'a',
    active_configuration_id: 'a',
    total: 2,
  });
  asrApi.getActiveLocalModel.mockResolvedValue(null);
  asrApi.getStatus.mockResolvedValue({ deployment_mode: 'full' });
  speakerApi.getTranscriptionSettings.mockResolvedValue({
    diarization_source: 'default',
    diarization_asr_config_uuid: null,
  });
});

async function renderLoaded() {
  const result = render(ASRSettings, { props: { isAdmin: false } });
  await waitFor(() => expect(result.container.querySelectorAll('.config-card').length).toBe(2));
  return result;
}
const cardOf = (c: HTMLElement, uuid: string) =>
  Array.from(c.querySelectorAll<HTMLElement>('.config-card')).find(
    (el) => el.querySelector('.config-name')?.textContent?.trim() === `cfg-${uuid}`
  )!;
const toggleOf = (c: HTMLElement, uuid: string) =>
  cardOf(c, uuid).querySelector<HTMLInputElement>('[data-testid="asr-enabled-toggle"]')!;

describe('the per-configuration on/off switch', () => {
  it('shows each configuration as on or off', async () => {
    const { container } = await renderLoaded();

    expect(toggleOf(container, 'a').checked).toBe(true);
    expect(toggleOf(container, 'b').checked).toBe(false);
  });

  it('turns a configuration off through the API and drops the active badge', async () => {
    asrApi.setEnabled.mockResolvedValue(config('a', false));
    const { container } = await renderLoaded();

    await fireEvent.click(toggleOf(container, 'a'));

    await waitFor(() => expect(asrApi.setEnabled).toHaveBeenCalledWith('a', false));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('settings.asrProvider.enableOff', 3000)
    );
    expect(cardOf(container, 'a').querySelector('.active-badge')).toBeNull();
  });

  it('turns an off configuration on', async () => {
    asrApi.setEnabled.mockResolvedValue(config('b', true));
    const { container } = await renderLoaded();

    await fireEvent.click(toggleOf(container, 'b'));

    await waitFor(() => expect(asrApi.setEnabled).toHaveBeenCalledWith('b', true));
    await waitFor(() => expect(toggleOf(container, 'b').checked).toBe(true));
  });

  it('puts the switch back and names the reason when the server refuses', async () => {
    asrApi.setEnabled.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { code: 'local_asr_unavailable', message: 'raw server sentence' } },
      },
    });
    const { container } = await renderLoaded();

    await fireEvent.click(toggleOf(container, 'a'));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error.mock.calls[0][0]).not.toContain('raw server sentence');
    expect(toggleOf(container, 'a').checked).toBe(true);
    expect(cardOf(container, 'a').querySelector('.active-badge')).not.toBeNull();
  });
});
