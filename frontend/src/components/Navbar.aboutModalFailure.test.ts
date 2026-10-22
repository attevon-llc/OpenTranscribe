/**
 * A failed About chunk load must tell the user and let the next click retry, never a silent no-op.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

const h = vi.hoisted(() => ({ gate: null as unknown as import('svelte/store').Writable<unknown> }));

vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  h.gate = writable({ active: false, readOnly: false });
  return { supportSessionGate: h.gate };
});
vi.mock('$app/stores', async () => {
  const { readable } = await import('svelte/store');
  return { page: readable({ url: new URL('http://localhost/') }) };
});
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return {
    user: writable({ role: 'admin', email: 'a@example.com' }),
    logout: vi.fn(),
    fetchUserInfo: vi.fn().mockResolvedValue(undefined),
  };
});
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k), locale: readable('en') };
});
vi.mock('$stores/websocket', async () => {
  const { readable } = await import('svelte/store');
  return { unreadCount: readable(0) };
});
vi.mock('$stores/notificationsPanel', async () => {
  const { writable } = await import('svelte/store');
  return { showNotificationsPanel: writable(false), toggleNotificationsPanel: vi.fn() };
});
vi.mock('$stores/recording', async () => {
  const { readable } = await import('svelte/store');
  return {
    recordingStore: readable({ hasActiveRecording: false }),
    recordingManager: {},
  };
});
vi.mock('$stores/uploads', () => ({ uploadsStore: {} }));
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('./AboutModal.svelte', () => {
  throw new Error('chunk load failed');
});
vi.mock('$stores/settingsModalStore', () => ({ settingsModalStore: { open: vi.fn() } }));
vi.mock('$lib/prefetch', () => ({
  prefetchSpeakersData: vi.fn(),
}));
vi.mock('$lib/edition', () => ({ isCloudEdition: true }));
vi.mock('$lib/cloud', () => ({
  refreshUsage: vi.fn().mockResolvedValue(undefined),
  refreshBilling: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('$components/navbar/UserDropdown.svelte', async () => ({
  default: (await import('../test-mocks/EmptyComponent.svelte')).default,
}));

import Navbar from './Navbar.svelte';
import { toastStore } from '$stores/toast';
import { capabilities } from '$stores/capabilities';

beforeEach(() => {
  capabilities.set({
    edition: 'cloud',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  h.gate.set({ active: false, readOnly: false });
});

describe('Navbar About chunk failure', () => {
  it('shows an error toast and resets so the click can retry', async () => {
    const { container } = render(Navbar);
    await fireEvent.click(container.querySelector('button.logo-link')!);
    await vi.waitFor(() => expect(toastStore.error).toHaveBeenCalledWith('nav.aboutLoadFailed'));
    expect(screen.queryByText('about.title')).toBeNull();
  });
});
