import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  getLocalModelCatalog: vi.fn(),
  startLocalModelDownload: vi.fn(),
  cancelLocalModelDownload: vi.fn(),
}));
const asr = vi.hoisted(() => ({
  setLocalModel: vi.fn(),
  restartGpuWorker: vi.fn(),
  getActiveLocalModel: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));

vi.mock('$lib/api/localModels', () => api);
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asr }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
vi.mock('$stores/locale', () => ({
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import LocalModelLibrary from './LocalModelLibrary.svelte';

const model = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  display_name: id,
  repo_id: `Systran/faster-whisper-${id}`,
  approx_bytes: 3_100_000_000,
  vram_hint_gb: 10,
  language_support: 'multilingual',
  supports_translation: true,
  is_default: false,
  downloaded: false,
  partial: false,
  present_bytes: 0,
  is_active: false,
  ...over,
});

const catalog = (over: Record<string, unknown> = {}) => ({
  models: [
    model('large-v3'),
    model('tiny', { approx_bytes: 75_000_000, downloaded: true }),
    model('small', { downloaded: true, is_active: true }),
  ],
  download: null,
  free_bytes: 50_000_000_000,
  cache_writable: true,
  active_model: 'small',
  ...over,
});

const state = (over: Record<string, unknown> = {}) => ({
  model_id: 'large-v3',
  status: 'downloading',
  bytes_done: 400,
  bytes_total: 1000,
  percent: 40,
  error_code: null,
  updated_at: '2026-10-15T10:00:00Z',
  ...over,
});

const socket = (payload: Record<string, unknown>) =>
  window.dispatchEvent(new CustomEvent('local-model-download', { detail: payload }));

beforeEach(() => {
  vi.clearAllMocks();
  api.getLocalModelCatalog.mockResolvedValue(catalog());
});

afterEach(() => vi.useRealTimers());

describe('LocalModelLibrary', () => {
  it('is locked for an admin and makes no request', async () => {
    render(LocalModelLibrary, { props: { locked: true } });
    expect(await screen.findByTestId('local-model-locked-hint')).toBeInTheDocument();
    expect(api.getLocalModelCatalog).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /settings.localModels.download/ })).toBeNull();
  });

  it('lists every catalog model with its state and free disk space', async () => {
    render(LocalModelLibrary, { props: { locked: false } });
    expect(await screen.findByTestId('local-model-large-v3')).toBeInTheDocument();
    expect(screen.getByTestId('local-model-tiny')).toBeInTheDocument();
    expect(screen.getByText(/settings\.localModels\.freeSpace.*50 GB/)).toBeInTheDocument();
    // The active model offers no "Set active"; a model that is not on disk can only be downloaded.
    const small = screen.getByTestId('local-model-small');
    expect(
      within(small).queryByRole('button', { name: /settings.localModels.activate/ })
    ).toBeNull();
    const large = screen.getByTestId('local-model-large-v3');
    expect(
      within(large).getByRole('button', { name: /settings.localModels.activate/ })
    ).toBeDisabled();
  });

  it('starts the download of the chosen model', async () => {
    api.startLocalModelDownload.mockResolvedValue(state({ status: 'queued', percent: null }));
    render(LocalModelLibrary, { props: { locked: false } });
    const row = await screen.findByTestId('local-model-large-v3');
    await fireEvent.click(
      within(row).getByRole('button', { name: /settings.localModels.download/ })
    );
    expect(api.startLocalModelDownload).toHaveBeenCalledWith('large-v3');
    expect(
      await within(row).findByRole('button', { name: 'settings.localModels.cancel' })
    ).toBeVisible();
  });

  it('shows socket progress with a progressbar and a Cancel that calls the API', async () => {
    api.cancelLocalModelDownload.mockResolvedValue(state());
    render(LocalModelLibrary, { props: { locked: false } });
    const row = await screen.findByTestId('local-model-large-v3');

    socket({
      model_id: 'large-v3',
      status: 'downloading',
      bytes_done: 400_000_000,
      bytes_total: 1_000_000_000,
      percent: 40,
      error_code: null,
    });

    const bar = await within(row).findByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '40');
    expect(within(row).getByText(/settings.localModels.ofBytes.*400 MB.*1 GB/)).toBeInTheDocument();
    // Only one download at a time: the other rows cannot start one.
    const tiny = screen.getByTestId('local-model-small');
    expect(
      within(tiny).queryByRole('button', { name: /settings.localModels.download/ })
    ).toBeNull();

    await fireEvent.click(within(row).getByRole('button', { name: 'settings.localModels.cancel' }));
    expect(api.cancelLocalModelDownload).toHaveBeenCalledTimes(1);
  });

  it('renders a refused start as a fixed sentence with formatted numbers, not server text', async () => {
    api.startLocalModelDownload.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: {
            code: 'insufficient_disk_space',
            message: 'SERVER TEXT MUST NOT APPEAR',
            required_bytes: 3_100_000_000,
            free_bytes: 300_000_000,
          },
        },
      },
    });
    render(LocalModelLibrary, { props: { locked: false } });
    const row = await screen.findByTestId('local-model-large-v3');
    await fireEvent.click(
      within(row).getByRole('button', { name: /settings.localModels.download/ })
    );

    const alert = await within(row).findByRole('alert');
    expect(alert).toHaveTextContent(
      'settings.localModels.error.insufficient_disk_space:{"required":"3.1 GB","free":"300 MB"}'
    );
    expect(screen.queryByText(/SERVER TEXT/)).toBeNull();
  });

  it('re-reads the catalog every few seconds while a download runs and the socket is quiet, then stops', async () => {
    vi.useFakeTimers();
    api.getLocalModelCatalog.mockResolvedValue(catalog({ download: state() }));
    render(LocalModelLibrary, { props: { locked: false } });
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getLocalModelCatalog).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(3100);
    expect(api.getLocalModelCatalog).toHaveBeenCalledTimes(2);

    api.getLocalModelCatalog.mockResolvedValue(
      catalog({ download: state({ status: 'completed', percent: 100 }) })
    );
    await vi.advanceTimersByTimeAsync(3000);
    const callsAtFinish = api.getLocalModelCatalog.mock.calls.length;
    expect(toast.success).toHaveBeenCalledWith(
      'settings.localModels.toast.completed:{"model":"large-v3"}'
    );

    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.getLocalModelCatalog.mock.calls.length).toBe(callsAtFinish);
  });

  it('toasts and refreshes the catalog when the socket reports a failure', async () => {
    render(LocalModelLibrary, { props: { locked: false } });
    const row = await screen.findByTestId('local-model-large-v3');
    socket({
      model_id: 'large-v3',
      status: 'downloading',
      bytes_done: 1,
      bytes_total: 10,
      percent: 10,
      error_code: null,
    });
    await within(row).findByRole('progressbar');
    const before = api.getLocalModelCatalog.mock.calls.length;

    socket({
      model_id: 'large-v3',
      status: 'failed',
      bytes_done: 1,
      bytes_total: 10,
      percent: 10,
      error_code: 'hf_unreachable',
    });

    expect(await within(row).findByRole('alert')).toHaveTextContent(
      'settings.localModels.error.hf_unreachable'
    );
    expect(toast.error).toHaveBeenCalled();
    await waitFor(() => expect(api.getLocalModelCatalog.mock.calls.length).toBeGreaterThan(before));
  });
});

describe('LocalModelLibrary activation', () => {
  const reply = {
    status: 'restart_signaled',
    model: 'tiny',
    workers: ['gpu-transcription@a'],
    active_tasks: 2,
    restart_requested_at: '2026-10-15T10:00:00+00:00',
    message: 'x',
  };

  it('asks for confirmation that names the restart and does nothing until confirmed', async () => {
    render(LocalModelLibrary, { props: { locked: false } });
    const row = await screen.findByTestId('local-model-tiny');
    await fireEvent.click(
      within(row).getByRole('button', { name: /settings.localModels.activate/ })
    );

    expect(
      await screen.findByText('settings.localModels.activateDialog.message:{"model":"tiny"}')
    ).toBeInTheDocument();
    expect(asr.setLocalModel).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'modal.cancel' }));
    expect(asr.setLocalModel).not.toHaveBeenCalled();
    expect(asr.restartGpuWorker).not.toHaveBeenCalled();
  });

  it('sets, then restarts, shows the running jobs, and confirms from the worker report', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const order: string[] = [];
    asr.setLocalModel.mockImplementation(async () => void order.push('set'));
    asr.restartGpuWorker.mockImplementation(async () => (order.push('restart'), reply));
    asr.getActiveLocalModel.mockResolvedValueOnce({ loaded_models: [] }).mockResolvedValue({
      loaded_models: [
        { worker: 'gpu-transcription@a', model: 'tiny', ready_at: '2026-10-15T10:01:00+00:00' },
      ],
    });
    const onActivated = vi.fn();
    render(LocalModelLibrary, { props: { locked: false }, events: { activated: onActivated } });

    const row = await screen.findByTestId('local-model-tiny');
    await fireEvent.click(
      within(row).getByRole('button', { name: /settings.localModels.activate/ })
    );
    await fireEvent.click(
      await screen.findByRole('button', { name: 'settings.localModels.activateDialog.confirm' })
    );

    expect(await screen.findByText('settings.localModels.restarting')).toBeInTheDocument();
    expect(screen.getByText('settings.localModels.jobsRunning:{"count":2}')).toBeInTheDocument();
    expect(order).toEqual(['set', 'restart']);

    await vi.advanceTimersByTimeAsync(3100);
    await vi.advanceTimersByTimeAsync(3100);
    expect(
      await screen.findByText('settings.localModels.running:{"model":"tiny"}')
    ).toBeInTheDocument();
    expect(onActivated).toHaveBeenCalledTimes(1);
  });

  it('shows the fixed sentence and restarts nothing when the model was refused', async () => {
    asr.setLocalModel.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { code: 'local_model_not_downloaded', message: 'SERVER' } },
      },
    });
    render(LocalModelLibrary, { props: { locked: false } });
    const row = await screen.findByTestId('local-model-tiny');
    await fireEvent.click(
      within(row).getByRole('button', { name: /settings.localModels.activate/ })
    );
    await fireEvent.click(
      await screen.findByRole('button', { name: 'settings.localModels.activateDialog.confirm' })
    );

    expect(
      await screen.findByText('settings.localModels.error.local_model_not_downloaded')
    ).toBeInTheDocument();
    expect(asr.restartGpuWorker).not.toHaveBeenCalled();
  });
});
