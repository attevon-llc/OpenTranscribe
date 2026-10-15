/**
 * Owner-reported: Settings took ~1.1 s to open the first time because every section's code was
 * downloaded before anything painted. The contract now: the dialog, its sidebar and the section's
 * heading are there immediately, and the panel behind the heading loads asynchronously inside the
 * already-open modal, with a loading state and a retry if its chunk fails.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ role: string } | null>(null), readAccountLifecycle: () => null };
});
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
  locale: {
    subscribe: (run: (value: string) => void) => {
      run('en');
      return () => {};
    },
  },
}));

const mockUserSettingsApi = vi.hoisted(() => ({
  getRecordingSettings: vi.fn(),
  updateRecordingSettings: vi.fn(),
  resetRecordingSettings: vi.fn(),
}));
vi.mock('$lib/api/userSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/userSettings')>();
  return { ...actual, UserSettingsApi: mockUserSettingsApi };
});
vi.mock('$lib/api/userApprovals', () => ({
  UserApprovalsApi: { list: vi.fn().mockResolvedValue([]) },
  isAlreadyDecided: () => false,
}));

// Hold the Download panel's chunk open so the "loading" window is observable, and let the test
// decide whether it lands or fails. Everything else keeps its real loader.
const download = vi.hoisted(() => ({ load: vi.fn() }));
vi.mock('$lib/settings/sectionLoaders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/settings/sectionLoaders')>();
  return {
    ...actual,
    sectionLoaders: { ...actual.sectionLoaders, download: () => download.load() },
  };
});

import SettingsModal from './SettingsModal.svelte';
import Probe from './settings/LazySectionProbe.svelte';
import { user as mockUser } from '$stores/auth';
import { settingsModalStore } from '$stores/settingsModalStore';
import { capabilities } from '$stores/capabilities';
import { resetAppStores } from '../test-mocks/app-stores';

beforeEach(() => {
  vi.clearAllMocks();
  resetAppStores();
  settingsModalStore.reset();
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  mockAxios.get.mockResolvedValue({ data: {} });
  mockUserSettingsApi.getRecordingSettings.mockResolvedValue({
    max_recording_duration: 120,
    recording_quality: 'high',
    auto_stop_enabled: true,
  });
  (mockUser as unknown as { set: (v: { role: string }) => void }).set({ role: 'user' });
});

describe('Settings modal lazy sections', () => {
  it('shows the heading and a loading state at once, then the panel when its chunk lands', async () => {
    let land!: (m: { default: unknown }) => void;
    download.load.mockReturnValue(new Promise((resolve) => (land = resolve)));
    settingsModalStore.open('download');
    const { container } = render(SettingsModal);

    // Dialog, sidebar and the section heading are painted without waiting for the panel.
    expect(container.querySelector('.settings-modal')).not.toBeNull();
    expect(container.querySelector('.settings-sidebar .nav-item')).not.toBeNull();
    expect(container.querySelector('.settings-content .section-title')?.textContent).toBe(
      'settings.download.title'
    );
    expect(await screen.findByTestId('settings-section-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('lazy-probe')).toBeNull();

    land({ default: Probe });
    expect(await screen.findByTestId('lazy-probe')).toBeInTheDocument();
    expect(screen.queryByTestId('settings-section-loading')).toBeNull();
  });

  it('keeps the modal usable when a section chunk fails, and retries on demand', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    download.load
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce({ default: Probe });
    settingsModalStore.open('download');
    const { container } = render(SettingsModal);

    expect(await screen.findByRole('alert')).toHaveTextContent('settings.modal.sectionLoadFailed');
    // Still a working dialog: the sidebar is there and navigation is not blocked.
    expect(container.querySelector('.settings-sidebar .nav-item')).not.toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: 'settings.modal.sectionRetry' }));
    await waitFor(() => expect(screen.getByTestId('lazy-probe')).toBeInTheDocument());
    consoleError.mockRestore();
  });
});
