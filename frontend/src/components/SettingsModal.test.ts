/**
 * `SettingsModal.svelte` is the one place a section's required privilege tier
 * (`SECTION_MIN_ROLE`) turns into what actually renders: the sidebar, the mobile
 * picker, and the content router all key off the same `sectionLocked()` call, and
 * per `components/settings/CLAUDE.md` a section the user lacks privilege for must
 * render disabled-with-a-tooltip, never be omitted (an admin once concluded a page
 * did not exist because it was silently dropped). That gating, the unsaved-changes
 * close confirmation, and the mount-time badge/data orchestration are exactly the
 * "complex derived state and multi-step orchestration" this suite scopes to — the
 * ~40 child settings panels themselves are out of scope here (each is/should be
 * tested independently, and Playwright already exercises this modal end to end).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

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

const mockUserApprovalsApi = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock('$lib/api/userApprovals', () => ({
  UserApprovalsApi: mockUserApprovalsApi,
  isAlreadyDecided: () => false,
}));

import SettingsModal from './SettingsModal.svelte';
import { user as mockUser } from '$stores/auth';
import { settingsModalStore } from '$stores/settingsModalStore';
import { capabilities } from '$stores/capabilities';
import { resetAppStores } from '../test-mocks/app-stores';

function setUser(role: 'user' | 'admin' | 'super_admin' | null) {
  (mockUser as unknown as { set: (value: { role: string } | null) => void }).set(
    role ? { role } : null
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  resetAppStores();
  settingsModalStore.reset();
  capabilities.set({ edition: 'community', loaded: true, capabilities: {}, audience: {} });
  mockAxios.get.mockResolvedValue({ data: {} });
  mockAxios.post.mockResolvedValue({ data: {} });
  mockUserSettingsApi.getRecordingSettings.mockResolvedValue({
    max_recording_duration: 120,
    recording_quality: 'high',
    auto_stop_enabled: true,
  });
  mockUserApprovalsApi.list.mockResolvedValue([]);
  setUser('user');
});

/** Opens on 'recording' — an inline form section with no heavy child component,
 * so the sidebar's own gating/derived-state logic is under test in isolation. */
function openModal() {
  settingsModalStore.open('recording');
  return render(SettingsModal);
}

/** The nav label and the open panel's own heading share the same i18n key, so
 * a plain findByText is ambiguous — wait on the content pane's heading instead. */
async function waitForOpen(container: HTMLElement) {
  await waitFor(() =>
    expect(container.querySelector('.settings-content .section-title')).not.toBeNull()
  );
}

function navItem(container: HTMLElement, label: string): HTMLElement | null {
  return (
    (Array.from(container.querySelectorAll('.settings-sidebar .nav-item')).find(
      (el) => el.querySelector('.nav-item-label')?.textContent?.includes(label)
    ) as HTMLElement | undefined) ?? null
  );
}

describe('privilege-gated sidebar sections', () => {
  it('omits admin-only groups entirely for a plain user', async () => {
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);

    expect(navItem(container, 'settings.users.title')).toBeNull();
    expect(navItem(container, 'settings.authentication.title')).toBeNull();
  });

  it('renders a super-admin-only section disabled with a lock, not omitted, for a plain admin', async () => {
    setUser('admin');
    const { container } = openModal();
    await waitForOpen(container);

    const authItem = navItem(container, 'settings.authentication.title');
    expect(authItem).not.toBeNull();
    expect(authItem).toHaveAttribute('disabled');
    expect(authItem?.classList.contains('locked')).toBe(true);
    expect(authItem?.getAttribute('title')).toBe('settings.nav.requiresSuperAdmin');
    expect(authItem?.querySelector('.lock-indicator')).not.toBeNull();
  });

  it('unlocks the same section for a super admin', async () => {
    setUser('super_admin');
    const { container } = openModal();
    await waitForOpen(container);

    const authItem = navItem(container, 'settings.authentication.title');
    expect(authItem).not.toHaveAttribute('disabled');
    expect(authItem?.classList.contains('locked')).toBe(false);
  });

  it('drops a section entirely when its capability is disabled by the backend', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'asr.user_providers': false, 'prompts.user': false },
      audience: {},
    });
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);

    // Absent entirely — distinct from a privilege-gated item, which stays
    // present, disabled, and carrying a lock icon (checked in the test above).
    expect(navItem(container, 'settings.asrProvider.title')).toBeNull();
  });
});

describe('pending-approval badge', () => {
  it('fetches the count on mount for an admin and shows it on the Users nav item', async () => {
    mockUserApprovalsApi.list.mockResolvedValue([{ uuid: 'a' }, { uuid: 'b' }]);
    setUser('admin');
    const { container } = openModal();
    await waitForOpen(container);

    await waitFor(() => {
      const usersItem = navItem(container, 'settings.users.title');
      expect(usersItem?.querySelector('.nav-badge')?.textContent?.trim()).toBe('2');
    });
  });

  it('does not fetch the approval count for a non-admin', async () => {
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);

    expect(mockUserApprovalsApi.list).not.toHaveBeenCalled();
  });

  it('falls back to zero (no badge) when the count fetch fails, rather than a stuck phantom count', async () => {
    mockUserApprovalsApi.list.mockRejectedValue(new Error('boom'));
    setUser('admin');
    const { container } = openModal();
    await waitForOpen(container);

    await waitFor(() => expect(mockUserApprovalsApi.list).toHaveBeenCalled());
    expect(navItem(container, 'settings.users.title')?.querySelector('.nav-badge')).toBeNull();
  });
});

describe('unsaved-changes close confirmation', () => {
  it('closes immediately via Escape when nothing is dirty', async () => {
    const { container } = openModal();
    await waitForOpen(container);

    await fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => expect(container.querySelector('.settings-modal')).toBeNull());
  });

  it('shows a confirmation instead of closing when a section has unsaved changes, and force-closes on confirm', async () => {
    const { container, getByText } = openModal();
    await waitForOpen(container);

    settingsModalStore.setDirty('recording', true);
    const closeBtn = container.querySelector('.modal-close-button') as HTMLElement;
    await fireEvent.click(closeBtn);

    // The settings dialog is still open, plus a confirmation surfaced.
    expect(container.querySelector('.settings-modal')).not.toBeNull();
    const confirmBtn = getByText('settings.closeWithoutSaving') as HTMLElement;
    await fireEvent.click(confirmBtn);

    await waitFor(() => expect(container.querySelector('.settings-modal')).toBeNull());
  });

  it('cancelling the confirmation leaves the modal open and the section still dirty', async () => {
    const { container, getByText } = openModal();
    await waitForOpen(container);

    settingsModalStore.setDirty('recording', true);
    await fireEvent.click(container.querySelector('.modal-close-button') as HTMLElement);
    const cancelBtn = getByText('settings.keepEditing') as HTMLElement;
    await fireEvent.click(cancelBtn);

    expect(container.querySelector('.settings-modal')).not.toBeNull();
    let dirty = false;
    settingsModalStore.subscribe((s) => (dirty = s.dirtyState.recording))();
    expect(dirty).toBe(true);
  });
});

describe('section-switch data loading', () => {
  it('fetches system stats when the System Statistics section is opened', async () => {
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);
    mockAxios.get.mockClear();

    const statsItem = navItem(container, 'settings.statistics.title') as HTMLElement;
    await fireEvent.click(statsItem);

    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/system/stats'));
  });
});
