/**
 * `NotificationsPanel` was mounted twice: once by `routes/+layout.svelte` (always, so it
 * outlives the navbar) and once by `Navbar.svelte` (only while open). Both subscribe to the same
 * open/closed store, so while the panel was open the page held two copies of its markup, two
 * click-outside listeners and two scroll locks. The layout mount is the one that is needed —
 * the panel is app chrome, not part of the bar — so the navbar must not mount one.
 *
 * The first two tests render the REAL panel (not a stub) beside the REAL navbar, the way the
 * layout composes them; a stubbed panel would pass whether or not the navbar mounted one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  return { supportSessionGate: writable({ active: false, readOnly: false }) };
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
    token: writable('t'),
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
  return {
    unreadCount: readable(0),
    websocketStore: {
      ...readable({ notifications: [] }),
      removeNotification: vi.fn(),
      markAsRead: vi.fn(),
      markAllAsRead: vi.fn(),
      clearAll: vi.fn(),
    },
  };
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

import Navbar from './Navbar.svelte';
import NotificationsPanel from './NotificationsPanel.svelte';
import { showNotificationsPanel } from '../stores/notificationsPanel';

beforeEach(() => {
  showNotificationsPanel.set(false);
});

describe('the navbar does not re-fetch a user the session probe / login already loaded', () => {
  it('mounting issues no GET /auth/me', async () => {
    const { fetchUserInfo } = await import('$stores/auth');
    vi.mocked(fetchUserInfo).mockClear();

    const { container } = render(Navbar);

    expect(container.querySelector('.notifications-btn')).toBeTruthy(); // control: navbar rendered
    expect(fetchUserInfo).not.toHaveBeenCalled();
  });
});

describe('the notifications panel is mounted once', () => {
  it('the navbar alone renders no panel even while the open flag is set', async () => {
    const { container } = render(Navbar);
    expect(container.querySelector('.notifications-btn')).toBeTruthy(); // control: navbar rendered
    showNotificationsPanel.set(true);
    await tick();
    expect(document.querySelectorAll('.notifications-panel')).toHaveLength(0);
  });

  it('navbar plus the layout-level panel show exactly one panel and one backdrop', async () => {
    render(Navbar);
    render(NotificationsPanel);
    showNotificationsPanel.set(true);
    await tick();
    expect(document.querySelectorAll('.notifications-panel')).toHaveLength(1);
    expect(document.querySelectorAll('.notifications-backdrop')).toHaveLength(1);
  });

  it('only routes/+layout.svelte mounts <NotificationsPanel> anywhere in src', () => {
    const root = join(__dirname, '..');
    const mounts: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) walk(path);
        else if (
          name.endsWith('.svelte') &&
          /<NotificationsPanel[\s/>]/.test(readFileSync(path, 'utf8'))
        )
          mounts.push(path.slice(root.length + 1));
      }
    };
    walk(root);
    expect(mounts).toEqual(['routes/+layout.svelte']);
  });
});
