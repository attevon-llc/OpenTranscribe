import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const asrApi = vi.hoisted(() => ({
  getProviders: vi.fn(),
  getSettings: vi.fn(),
  getActiveLocalModel: vi.fn(),
  getStatus: vi.fn(),
  setActive: vi.fn(),
  clearActive: vi.fn(),
  deleteConfig: vi.fn(),
  deleteAll: vi.fn(),
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
import { issue } from '$lib/settings/pipelineViewFixtures';

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
const config = (uuid: string) => ({
  id: 1,
  uuid,
  user_id: 1,
  name: `cfg-${uuid}`,
  provider: 'deepgram',
  model_name: 'nova-3',
  is_active: true,
  has_api_key: true,
  is_shared: false,
  created_at: '',
  updated_at: '',
});

beforeEach(() => {
  vi.clearAllMocks();
  asrApi.getProviders.mockResolvedValue({ providers: PROVIDERS });
  asrApi.getSettings.mockResolvedValue({
    configurations: [config('a'), config('b')],
    shared_configurations: [],
    active_configuration_uuid: 'a',
    active_configuration_id: 'a',
    total: 2,
  });
  asrApi.getActiveLocalModel.mockResolvedValue(null);
  asrApi.getStatus.mockResolvedValue({ deployment_mode: 'full' });
  speakerApi.getTranscriptionSettings.mockResolvedValue({
    diarization_source: 'asr_config',
    diarization_asr_config_uuid: 'b',
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
const adapter = (c: HTMLElement, uuid: string) =>
  cardOf(c, uuid).querySelector('[data-testid="badge-adapter"]');
const conflict = (code: string) => ({
  response: { status: 409, data: { detail: { code, message: 'raw server sentence' } } },
});
async function confirmDelete() {
  await fireEvent.click(
    await waitFor(() => {
      const el = document.querySelector('.modal-delete-button');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    })
  );
}

describe('deleting a config that was the speaker detector', () => {
  it('says speaker detection is back to Automatic and drops the extra-billing badge', async () => {
    asrApi.deleteConfig.mockResolvedValue({
      deleted: true,
      diarizer_source_reverted: true,
      pipeline_issues: [],
      message: 'ok',
      config_uuid: 'a',
    });
    const { container } = await renderLoaded();
    expect(adapter(container, 'b')).not.toBeNull();

    await fireEvent.click(cardOf(container, 'b').querySelector('.btn-delete') as HTMLElement);
    await confirmDelete();

    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith('settings.asrProvider.diarizerReverted', 8000)
    );
    expect(container.querySelector('[data-testid="badge-adapter"]')).toBeNull();
  });

  it('names a problem the deletion leaves, from its code and not the server text', async () => {
    asrApi.deleteConfig.mockResolvedValue({
      deleted: true,
      diarizer_source_reverted: false,
      pipeline_issues: [
        issue('asr_no_timing_for_speakers', 'error'),
        issue('speaker_hints_ignored', 'warning'),
      ],
      message: 'ok',
      config_uuid: null,
    });
    const { container } = await renderLoaded();
    await fireEvent.click(cardOf(container, 'b').querySelector('.btn-delete') as HTMLElement);
    await confirmDelete();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'settings.pipeline.issue.asr_no_timing_for_speakers',
        8000
      )
    );
    expect(toast.warning).not.toHaveBeenCalled();
    expect(JSON.stringify(toast.error.mock.calls)).not.toContain('raw server sentence');
  });

  it('explains a lite refusal to delete the last provider by its code', async () => {
    asrApi.deleteConfig.mockRejectedValue(conflict('local_asr_unavailable'));
    const { container } = await renderLoaded();
    await fireEvent.click(cardOf(container, 'b').querySelector('.btn-delete') as HTMLElement);
    await confirmDelete();
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'settings.pipeline.issue.local_asr_unavailable',
        5000
      )
    );
  });
});

describe('switching the active provider', () => {
  it('reports what the new provider does to speaker detection', async () => {
    asrApi.setActive.mockResolvedValue({
      message: 'ok',
      config_uuid: 'b',
      pipeline_issues: [issue('segment_timing_only', 'warning')],
    });
    const { container } = await renderLoaded();
    await fireEvent.click(cardOf(container, 'b').querySelector('.btn-activate') as HTMLElement);
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith(
        'settings.pipeline.issue.segment_timing_only',
        8000
      )
    );
  });

  it('shows the refusal sentence for its code when the switch cannot run', async () => {
    asrApi.setActive.mockRejectedValue(conflict('asr_no_timing_for_speakers'));
    const { container } = await renderLoaded();
    await fireEvent.click(cardOf(container, 'b').querySelector('.btn-activate') as HTMLElement);
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'settings.pipeline.issue.asr_no_timing_for_speakers',
        5000
      )
    );
  });

  it('falls back to the generic message for a refusal with no known code', async () => {
    asrApi.setActive.mockRejectedValue(conflict('unheard_of'));
    const { container } = await renderLoaded();
    await fireEvent.click(cardOf(container, 'b').querySelector('.btn-activate') as HTMLElement);
    await waitFor(() => expect(toast.error).toHaveBeenCalledOnce());
    expect(toast.error.mock.calls[0][0]).not.toContain('raw server sentence');
  });
});
