/**
 * The navbar logo opens the About (credits) modal, including in the cloud build.
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

describe('Navbar logo opens the About modal (cloud edition build)', () => {
  it('shows the credits dialog on the first click', async () => {
    const { container } = render(Navbar);
    await fireEvent.click(container.querySelector('button.logo-link')!);
    expect(await screen.findByText('about.title')).toBeTruthy();
  });
});
