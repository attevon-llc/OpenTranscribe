/**
 * Opening the bell from the mobile (hamburger) menu must close that menu.
 *
 * DEFECT THIS CATCHES: at phone width the bell lives inside the open menu (z-index 1100),
 * and the notifications panel (z-index 1000) opened BEHIND it: the user saw the menu with
 * a grey overlay and no panel, and could not reach the panel without closing the menu.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

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
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$lib/cloud', () => ({
  refreshUsage: vi.fn().mockResolvedValue(undefined),
  refreshBilling: vi.fn().mockResolvedValue(undefined),
}));

import Navbar from './Navbar.svelte';

beforeEach(() => {
  h.gate.set({ active: false, readOnly: false });
});

describe('Navbar mobile menu and the notifications bell', () => {
  it('closes the mobile menu when the bell is pressed', async () => {
    const { container } = render(Navbar);
    const links = () => container.querySelector('.nav-links') as HTMLElement;

    await fireEvent.click(container.querySelector('.mobile-toggle button')!);
    expect(links().classList.contains('open')).toBe(true);

    await fireEvent.click(container.querySelector('.notifications-btn')!);
    expect(links().classList.contains('open')).toBe(false);
  });
});
