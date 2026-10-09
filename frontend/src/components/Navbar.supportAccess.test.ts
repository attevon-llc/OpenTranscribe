/**
 * While a support session is active the navbar must not offer search, chat or the speaker
 * manager: the backend refuses all of them under a grant (issue #1122), so the entries are
 * hidden instead of failing. The control (no session) proves the links are normally there,
 * which keeps the "absent" assertions from passing on a navbar that never rendered them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

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
vi.mock('$stores/gallery', async () => {
  const { readable } = await import('svelte/store');
  return { galleryStore: {}, galleryState: readable({}) };
});
vi.mock('$stores/settingsModalStore', () => ({ settingsModalStore: { open: vi.fn() } }));
vi.mock('$lib/prefetch', () => ({
  prefetchSpeakersData: vi.fn(),
  prefetchFileStatusData: vi.fn(),
}));
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$lib/cloud', () => ({ refreshUsage: vi.fn(), refreshBilling: vi.fn() }));
vi.mock('$components/navbar/UserDropdown.svelte', async () => ({
  default: (await import('../test-mocks/EmptyComponent.svelte')).default,
}));
vi.mock('./NotificationsPanel.svelte', async () => ({
  default: (await import('../test-mocks/EmptyComponent.svelte')).default,
}));

import Navbar from './Navbar.svelte';
import { capabilities } from '$stores/capabilities';

beforeEach(() => {
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  h.gate.set({ active: false, readOnly: false });
});

describe('Navbar entries under a support session', () => {
  it('shows gallery, search, chat and speakers normally', () => {
    render(Navbar);
    for (const id of ['nav-gallery', 'nav-search', 'nav-chat', 'nav-speakers']) {
      expect(screen.getByTestId(id)).toBeTruthy();
    }
  });

  it('hides search, chat and speakers during a session but keeps the gallery', () => {
    h.gate.set({ active: true, readOnly: true });
    render(Navbar);
    expect(screen.getByTestId('nav-gallery')).toBeTruthy();
    expect(screen.queryByTestId('nav-search')).toBeNull();
    expect(screen.queryByTestId('nav-chat')).toBeNull();
    expect(screen.queryByTestId('nav-speakers')).toBeNull();
  });
});
