/**
 * Factories and fixtures shared by the `SettingsModal.*` suites. `vi.mock` is hoisted and stays in
 * each test file; the factory bodies live here: `vi.mock('$stores/auth', async () =>
 * (await import('$testMocks/settingsModalMocks')).authUserMock())`.
 */
import { vi } from 'vitest';
import { writable } from 'svelte/store';

/** `$stores/auth` as the modal reads it: a settable `user` store and no account-lifecycle state. */
export const authUserMock = () => ({
  user: writable<{ role: string; org_role?: string } | null>(null),
  readAccountLifecycle: () => null,
});

/** `$lib/axios` as the settings panels import it. */
export const settingsAxiosMock = (instance: object) => ({
  default: instance,
  isRequestCancelled: () => false,
});

/** `$lib/api/userApprovals` with an empty queue. */
export const userApprovalsMock = () => ({
  UserApprovalsApi: { list: vi.fn().mockResolvedValue([]) },
  isAlreadyDecided: () => false,
});

export const RECORDING_SETTINGS = {
  max_recording_duration: 120,
  recording_quality: 'high',
  auto_stop_enabled: true,
};
