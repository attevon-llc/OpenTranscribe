/**
 * The admin YouTube policy card (issue #566): it loads the stored policy, sends only a
 * valid body on save, and disables the dependent numbers while their switch is off.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', async () => (await import('$testMocks/serviceMocks')).axiosInstanceMock());
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import YouTubePolicySettings from './YouTubePolicySettings.svelte';

const POLICY = {
  user_rate_limit_enabled: true,
  user_rate_limit_per_hour: 50,
  user_rate_limit_per_day: 500,
  auto_retry_enabled: true,
  recovery_batch_size: 3,
};

describe('YouTubePolicySettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: POLICY });
  });

  it('shows the stored values and keeps Save disabled until something changes', async () => {
    render(YouTubePolicySettings);
    const hour = (await screen.findByDisplayValue('50')) as HTMLInputElement;
    expect(hour.id).toBe('youtube-per-hour');
    expect(axiosInstance.get).toHaveBeenCalledWith('/admin/youtube-settings');
    expect(
      (screen.getByText('settings.youtube.save') as HTMLButtonElement).closest('button')!.disabled
    ).toBe(true);
  });

  it('saves the edited policy and confirms it', async () => {
    vi.mocked(axiosInstance.put).mockResolvedValue({
      data: { ...POLICY, user_rate_limit_per_hour: 20 },
    });
    render(YouTubePolicySettings);
    const hour = await screen.findByDisplayValue('50');
    await fireEvent.input(hour, { target: { value: '20' } });
    await fireEvent.click(screen.getByText('settings.youtube.save'));
    await waitFor(() =>
      expect(axiosInstance.put).toHaveBeenCalledWith(
        '/admin/youtube-settings',
        expect.objectContaining({ user_rate_limit_per_hour: 20, recovery_batch_size: 3 })
      )
    );
    expect(toastStore.success).toHaveBeenCalledWith('settings.youtube.saved');
  });

  it('disables the quota numbers when the limit switch is off', async () => {
    render(YouTubePolicySettings);
    const hour = (await screen.findByDisplayValue('50')) as HTMLInputElement;
    await fireEvent.click(document.getElementById('youtube-rate-limit')!);
    expect(hour.disabled).toBe(true);
  });

  it('reports a rejected save', async () => {
    vi.mocked(axiosInstance.put).mockRejectedValue(new Error('boom'));
    render(YouTubePolicySettings);
    const hour = await screen.findByDisplayValue('50');
    await fireEvent.input(hour, { target: { value: '10' } });
    await fireEvent.click(screen.getByText('settings.youtube.save'));
    await waitFor(() => expect(toastStore.error).toHaveBeenCalled());
  });
});
