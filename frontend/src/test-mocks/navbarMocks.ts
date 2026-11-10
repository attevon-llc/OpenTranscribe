/**
 * Module factories for the stores and libs `Navbar.svelte` pulls in. `vi.mock` is hoisted,
 * so each Navbar test file still declares its own `vi.mock(path, factory)`; the factory
 * bodies live here so the five Navbar suites do not each carry a copy.
 */
import { vi } from 'vitest';
import { readable, writable } from 'svelte/store';
import { capabilities } from '$stores/capabilities';

/** The one gate instance behind the mocked `$stores/supportSession`; tests drive it directly. */
export const supportGate = writable({ active: false, readOnly: false });

export const supportSessionMock = () => ({ supportSessionGate: supportGate });

export const authMock = (extra: Record<string, unknown> = {}) => ({
  user: writable({ role: 'admin', email: 'a@example.com' }),
  logout: vi.fn(),
  fetchUserInfo: vi.fn().mockResolvedValue(undefined),
  ...extra,
});

export const localeMock = () => ({ t: readable((k: string) => k), locale: readable('en') });

export const websocketMock = (extra: Record<string, unknown> = {}) => ({
  unreadCount: readable(0),
  ...extra,
});

export const notificationsPanelMock = () => ({
  showNotificationsPanel: writable(false),
  toggleNotificationsPanel: vi.fn(),
});

export const recordingMock = () => ({
  recordingStore: readable({ hasActiveRecording: false }),
  recordingManager: {},
});

export const cloudMock = () => ({
  refreshUsage: vi.fn().mockResolvedValue(undefined),
  refreshBilling: vi.fn().mockResolvedValue(undefined),
});

/** Loaded capabilities for `edition` with no gated features, and no support session. */
export const resetNavbarState = (edition: 'cloud' | 'community') => {
  capabilities.set({
    edition,
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  supportGate.set({ active: false, readOnly: false });
};
