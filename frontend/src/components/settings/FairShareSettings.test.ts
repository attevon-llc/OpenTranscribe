/**
 * Settings > File status > Recovery > Fair sharing between users: the admin control for
 * `transcription.fair_share_threshold`, read and written through the retry-config endpoint
 * (`GET`/`PUT /admin/settings/retry-config`).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);
const api = vi.hoisted(() => ({ getRetryConfig: vi.fn(), updateRetryConfig: vi.fn() }));
vi.mock('$lib/api/adminSettings', () => ({ AdminSettingsApi: api }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import FairShareSettings from './FairShareSettings.svelte';

const CONFIG = { max_retries: 3, retry_limit_enabled: true, fair_share_threshold: 5 };

beforeEach(() => {
  vi.clearAllMocks();
  api.getRetryConfig.mockResolvedValue(CONFIG);
});

async function loaded() {
  const view = render(FairShareSettings);
  const input = (await view.findByLabelText('settings.fairShare.label')) as HTMLInputElement;
  return { view, input, save: view.getByRole('button', { name: 'settings.retry.save' }) };
}

describe('FairShareSettings', () => {
  it('shows the stored threshold and leaves Save disabled until it changes', async () => {
    const { view, input, save } = await loaded();

    expect(input.value).toBe('5');
    expect(save).toBeDisabled();
    expect(view.getByText('settings.fairShare.help')).toBeInTheDocument();
  });

  it('saves only the threshold through the retry-config endpoint', async () => {
    api.updateRetryConfig.mockResolvedValue({ ...CONFIG, fair_share_threshold: 12 });
    const { view, input, save } = await loaded();

    await fireEvent.input(input, { target: { value: '12' } });
    await fireEvent.click(save);

    await waitFor(() =>
      expect(api.updateRetryConfig).toHaveBeenCalledWith({ fair_share_threshold: 12 })
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('settings.fairShare.saved'));
    expect(view.getByRole('button', { name: 'settings.retry.save' })).toBeDisabled();
  });

  it('explains that 0 turns fair sharing off', async () => {
    const { view, input } = await loaded();

    await fireEvent.input(input, { target: { value: '0' } });

    expect(view.getByText('settings.fairShare.offHint')).toBeInTheDocument();
    expect(view.queryByText('settings.fairShare.help')).not.toBeInTheDocument();
  });

  it('refuses to save a value outside 0 to 1000', async () => {
    const { input, save } = await loaded();

    await fireEvent.input(input, { target: { value: '1001' } });

    expect(save).toBeDisabled();
    expect(api.updateRetryConfig).not.toHaveBeenCalled();
  });

  it('reports a failed load instead of showing a made-up value as saved', async () => {
    api.getRetryConfig.mockRejectedValue(new Error('boom'));
    render(FairShareSettings);

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('settings.fairShare.loadFailed'));
  });
});
