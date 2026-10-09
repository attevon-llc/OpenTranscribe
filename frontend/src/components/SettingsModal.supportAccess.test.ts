/**
 * Support-access navigation (issue #1122). The two rows exist only when the backend says
 * `tenancy_mode === 'multi'`, and that gate is FAIL-CLOSED: the capabilities store itself
 * is fail-open (an unknown key reads as enabled), so a future refactor that swaps the
 * explicit `tenancyMode` check for `isCapabilityEnabled` would put support access on every
 * community install whose capabilities fetch failed. These tests pin that.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ role: string } | null>(null), readAccountLifecycle: () => null };
});
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
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
const supportApi = vi.hoisted(() => ({ listRequests: vi.fn() }));
vi.mock('$lib/api/supportAccess', () => ({ SupportAccessApi: supportApi }));
vi.mock('$lib/api/userApprovals', () => ({
  UserApprovalsApi: { list: vi.fn().mockResolvedValue([]) },
  isAlreadyDecided: () => false,
}));

import SettingsModal from './SettingsModal.svelte';
import { user as mockUser } from '$stores/auth';
import { settingsModalStore } from '$stores/settingsModalStore';
import { capabilities } from '$stores/capabilities';
import { resetAppStores } from '../test-mocks/app-stores';

function setUser(role: 'user' | 'admin' | 'super_admin') {
  (mockUser as unknown as { set: (value: { role: string }) => void }).set({ role });
}

function setMode(tenancyMode: 'single' | 'multi' | undefined) {
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
    tenancyMode,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  resetAppStores();
  settingsModalStore.reset();
  mockAxios.get.mockResolvedValue({ data: {} });
  supportApi.listRequests.mockResolvedValue({ items: [], total: 0, server_time: 'x' });
  mockUserSettingsApi.getRecordingSettings.mockResolvedValue({
    max_recording_duration: 120,
    recording_quality: 'high',
    auto_stop_enabled: true,
  });
});

async function open() {
  settingsModalStore.open('recording');
  const view = render(SettingsModal);
  await waitFor(() =>
    expect(view.container.querySelector('.settings-content .section-title')).not.toBeNull()
  );
  return view.container;
}

function navLabels(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.settings-sidebar .nav-item-label')).map(
    (el) => el.textContent?.trim() ?? ''
  );
}

describe('support-access nav row', () => {
  it('is absent while the tenancy mode is unknown, even for a super admin (fail closed)', async () => {
    setUser('super_admin');
    setMode(undefined);
    expect(navLabels(await open())).not.toContain('settings.supportAccess.navLabel');
  });

  it('is absent in single-tenant mode', async () => {
    setUser('super_admin');
    setMode('single');
    expect(navLabels(await open())).not.toContain('settings.supportAccess.navLabel');
  });

  it('is present in multi-tenant mode for an admin', async () => {
    setUser('admin');
    setMode('multi');
    expect(navLabels(await open())).toContain('settings.supportAccess.navLabel');
  });

  it('is not shown to a plain user even in multi-tenant mode (it lives in the admin group)', async () => {
    setUser('user');
    setMode('multi');
    expect(navLabels(await open())).not.toContain('settings.supportAccess.navLabel');
  });
});

describe('support-access requests nav row (everyone decides for their own workspace)', () => {
  it('is absent while the mode is unknown or single, for every role', async () => {
    for (const mode of [undefined, 'single'] as const) {
      setUser('super_admin');
      setMode(mode);
      const { unmount } = render(SettingsModal);
      settingsModalStore.open('recording');
      await waitFor(() => expect(document.querySelector('.settings-sidebar')).not.toBeNull());
      expect(navLabels(document.body)).not.toContain('settings.supportAccessRequests.navLabel');
      unmount();
      settingsModalStore.reset();
    }
  });

  it('is shown to a plain user in multi-tenant mode, who sees only this row, not the staff one', async () => {
    setUser('user');
    setMode('multi');
    const labels = navLabels(await open());
    expect(labels).toContain('settings.supportAccessRequests.navLabel');
    expect(labels).not.toContain('settings.supportAccess.navLabel');
  });

  it('an admin sees both rows', async () => {
    setUser('admin');
    setMode('multi');
    const labels = navLabels(await open());
    expect(labels).toContain('settings.supportAccessRequests.navLabel');
    expect(labels).toContain('settings.supportAccess.navLabel');
  });

  it('badges the row with the pending count and counts only the personal workspace without the org role', async () => {
    supportApi.listRequests.mockResolvedValue({ items: [], total: 3, server_time: 'x' });
    setUser('user');
    setMode('multi');
    const container = await open();
    await waitFor(() => {
      const row = Array.from(container.querySelectorAll('.settings-sidebar .nav-item')).find(
        (el) => el.textContent?.includes('settings.supportAccessRequests.navLabel')
      );
      expect(row?.textContent).toContain('3');
    });
    expect(supportApi.listRequests).toHaveBeenCalledWith('workspace', {
      status: 'pending',
      limit: 1,
      offset: 0,
    });
    expect(supportApi.listRequests).not.toHaveBeenCalledWith('org', expect.anything());
  });
});
