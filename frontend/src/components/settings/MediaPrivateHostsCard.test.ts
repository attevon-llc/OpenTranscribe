/** The super-admin private media host allowlist card: one host per line, trimmed on save. */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import MediaPrivateHostsCard from './MediaPrivateHostsCard.svelte';

describe('MediaPrivateHostsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: { hosts: ['http://10.0.0.5:8080'] } });
  });

  it('lists the stored hosts one per line in a left-to-right field', async () => {
    render(MediaPrivateHostsCard);
    const field = (await screen.findByDisplayValue('http://10.0.0.5:8080')) as HTMLTextAreaElement;
    expect(field.getAttribute('dir')).toBe('ltr');
    expect(axiosInstance.get).toHaveBeenCalledWith('/admin/media-private-hosts');
  });

  it('sends trimmed non-empty lines and confirms the save', async () => {
    vi.mocked(axiosInstance.put).mockResolvedValue({
      data: { hosts: ['http://10.0.0.5:8080', 'https://media.lan'] },
    });
    render(MediaPrivateHostsCard);
    const field = await screen.findByDisplayValue('http://10.0.0.5:8080');
    await fireEvent.input(field, {
      target: { value: 'http://10.0.0.5:8080\n  media.lan \n\n' },
    });
    await fireEvent.click(screen.getByText('settings.mediaSources.privateHosts.save'));
    await waitFor(() =>
      expect(axiosInstance.put).toHaveBeenCalledWith('/admin/media-private-hosts', {
        hosts: ['http://10.0.0.5:8080', 'media.lan'],
      })
    );
    expect(toastStore.success).toHaveBeenCalledWith('settings.mediaSources.privateHosts.saved');
  });
});
