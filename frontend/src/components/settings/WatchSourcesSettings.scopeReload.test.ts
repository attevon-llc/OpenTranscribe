/**
 * A scope switch (Mine / All) reloads the source list outside loadAll's try/catch. A failed
 * reload used to leave the previous scope's sources on screen under the new scope's button,
 * with no error and an unhandled rejection; it now clears the list and shows the load error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  getCapabilities: vi.fn(),
  getWatchSources: vi.fn(),
  deleteWatchSource: vi.fn(),
  updateWatchSource: vi.fn(),
  testWatchSource: vi.fn(),
  scanWatchSource: vi.fn(),
  getWatchSourceStats: vi.fn(),
  getEmailConfigs: vi.fn(),
  deleteEmailConfig: vi.fn(),
  testEmailConfig: vi.fn(),
  getGlobalSettings: vi.fn(),
  updateGlobalSettings: vi.fn(),
}));
// The real module for the pure label helpers the source card uses; the endpoints are spies.
vi.mock('$lib/api/watchSourcesApi', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...api,
}));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ role: string } | null>(null) };
});
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
// Modals and the heavy children are covered elsewhere; the shell is what is under test.
vi.mock('$components/settings/WatchSourceModal.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/EmailConfigModal.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/watchSources/WatchSourceFilesModal.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/watchSources/WatchSourceEmailLinksModal.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/ConfirmationModal.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));

import WatchSourcesSettings from './WatchSourcesSettings.svelte';
import { toastStore } from '$stores/toast';
import { user } from '$stores/auth';

const setRole = (role: string) => (user as unknown as { set: (v: unknown) => void }).set({ role });

beforeEach(() => {
  vi.clearAllMocks();
  api.getCapabilities.mockResolvedValue({
    watch_source_enabled: true,
    local_enabled: true,
    fs_events_enabled: false,
    fs_events_mode: 'auto',
  });
  api.getWatchSources.mockResolvedValue([]);
  api.getEmailConfigs.mockResolvedValue([]);
  api.getGlobalSettings.mockResolvedValue({
    enabled: true,
    file_stability_seconds: 5,
  });
});

const MINE = {
  uuid: 'src-mine',
  name: 'My drop folder',
  source_type: 'local',
  enabled: true,
  config: { path: '/watch/mine' },
};

describe('WatchSourcesSettings scope reload', () => {
  it('clears the old list and reports the error when the new scope fails to load', async () => {
    setRole('admin');
    api.getWatchSources.mockResolvedValueOnce([MINE]);
    api.getWatchSourceStats.mockResolvedValue(null);
    render(WatchSourcesSettings);
    expect(await screen.findByText('My drop folder')).toBeInTheDocument(); // control: list shown

    api.getWatchSources.mockRejectedValueOnce(new Error('503'));
    await fireEvent.click(screen.getByRole('button', { name: 'settings.watchSources.allScope' }));

    await waitFor(() => expect(toastStore.error).toHaveBeenCalledTimes(1));
    expect(api.getWatchSources).toHaveBeenLastCalledWith('all');
    expect(screen.queryByText('My drop folder')).not.toBeInTheDocument();
  });
});
