/**
 * Opening the bell from the mobile (hamburger) menu must close that menu.
 *
 * DEFECT THIS CATCHES: at phone width the bell lives inside the open menu (z-index 1100),
 * and the notifications panel (z-index 1000) opened BEHIND it: the user saw the menu with
 * a grey overlay and no panel, and could not reach the panel without closing the menu.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/supportSession', async () =>
  (await import('$testMocks/navbarMocks')).supportSessionMock()
);
vi.mock('$app/state', () => ({ page: { url: new URL('http://localhost/') } }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$stores/auth', async () => (await import('$testMocks/navbarMocks')).authMock());
vi.mock('$stores/locale', async () => (await import('$testMocks/navbarMocks')).localeMock());
vi.mock('$stores/websocket', async () => (await import('$testMocks/navbarMocks')).websocketMock());
vi.mock('$stores/notificationsPanel', async () =>
  (await import('$testMocks/navbarMocks')).notificationsPanelMock()
);
vi.mock('$stores/recording', async () => (await import('$testMocks/navbarMocks')).recordingMock());
vi.mock('$stores/uploads', () => ({ uploadsStore: {} }));
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('$stores/settingsModalStore', () => ({ settingsModalStore: { open: vi.fn() } }));
vi.mock('$lib/prefetch', () => ({
  prefetchSpeakersData: vi.fn(),
}));
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$lib/cloud', async () => (await import('$testMocks/navbarMocks')).cloudMock());

import Navbar from './Navbar.svelte';
import { supportGate } from '$testMocks/navbarMocks';

beforeEach(() => {
  supportGate.set({ active: false, readOnly: false });
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
