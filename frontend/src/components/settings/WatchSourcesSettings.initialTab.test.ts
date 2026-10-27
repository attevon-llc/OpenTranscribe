/**
 * Watch Sources can be opened on a chosen tab (#1210), and each tab owns a real tabpanel.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

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
vi.mock('$lib/api/watchSourcesApi', () => api);

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ role: string } | null>(null) };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
// Modals and the heavy children are covered elsewhere; the shell is what is under test.
vi.mock('$components/settings/WatchSourceModal.svelte', async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/EmailConfigModal.svelte', async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/watchSources/WatchSourceFilesModal.svelte', async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/watchSources/WatchSourceEmailLinksModal.svelte', async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/ConfirmationModal.svelte', async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));

import WatchSourcesSettings from './WatchSourcesSettings.svelte';
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

describe('WatchSourcesSettings initialTab', () => {
  it('opens the email tab and its panel for a super_admin', async () => {
    setRole('super_admin');
    render(WatchSourcesSettings, { props: { initialTab: 'email' } });

    const tab = await screen.findByRole('tab', { name: 'settings.emailNotifications.heading' });
    expect(tab).toHaveAttribute('aria-selected', 'true');
    const panel = screen.getByRole('tabpanel', { name: 'settings.emailNotifications.heading' });
    expect(panel).toHaveAttribute('id', 'tabpanel-email');
    expect(panel).toBeVisible();
    await waitFor(() => expect(panel).toHaveTextContent('settings.emailNotifications.heading'));
    expect(document.getElementById('tabpanel-sources')).not.toBeVisible();
  });

  it('defaults to the sources tab', async () => {
    setRole('super_admin');
    render(WatchSourcesSettings);
    const tab = await screen.findByRole('tab', { name: 'settings.watchSources.sourcesHeading' });
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(document.getElementById('tabpanel-email')).not.toBeVisible();
  });

  it('falls back to sources when a plain admin is deep-linked to a super_admin tab', async () => {
    setRole('admin');
    render(WatchSourcesSettings, { props: { initialTab: 'global' } });
    await waitFor(() => expect(api.getWatchSources).toHaveBeenCalled());
    expect(await screen.findByText('settings.watchSources.sourcesHeading')).toBeInTheDocument();
    expect(screen.queryByRole('tab')).toBeNull();
    expect(document.getElementById('tabpanel-global')).toBeNull();
    expect(api.getEmailConfigs).not.toHaveBeenCalled();
  });
});
