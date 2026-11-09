/**
 * While a support session is active the navbar must not offer search, chat or the speaker
 * manager: the backend refuses all of them under a grant (issue #1122), so the entries are
 * hidden instead of failing. The control (no session) proves the links are normally there,
 * which keeps the "absent" assertions from passing on a navbar that never rendered them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

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
vi.mock('$lib/cloud', () => ({ refreshUsage: vi.fn(), refreshBilling: vi.fn() }));
vi.mock('$components/navbar/UserDropdown.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));

import Navbar from './Navbar.svelte';
import { supportGate } from '$testMocks/navbarMocks';
import { capabilities } from '$stores/capabilities';

beforeEach(() => {
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  supportGate.set({ active: false, readOnly: false });
});

describe('Navbar entries under a support session', () => {
  it('shows gallery, search, chat and speakers normally', () => {
    render(Navbar);
    for (const id of ['nav-gallery', 'nav-search', 'nav-chat', 'nav-speakers']) {
      expect(screen.getByTestId(id)).toBeTruthy();
    }
  });

  it('hides search, chat and speakers during a session but keeps the gallery', () => {
    supportGate.set({ active: true, readOnly: true });
    render(Navbar);
    expect(screen.getByTestId('nav-gallery')).toBeTruthy();
    expect(screen.queryByTestId('nav-search')).toBeNull();
    expect(screen.queryByTestId('nav-chat')).toBeNull();
    expect(screen.queryByTestId('nav-speakers')).toBeNull();
  });
});

describe('Navbar destinations (issue #1243)', () => {
  it('has one home control and no File Status or All files tab', () => {
    const { container } = render(Navbar);
    const hrefs = Array.from(container.querySelectorAll('a.nav-link')).map((a) =>
      a.getAttribute('href')
    );
    expect(hrefs).toEqual(['/', '/search', '/chat', '/speakers']);
    expect(container.querySelector('.gallery-tabs')).toBeNull();
    expect(container.querySelector('a[href="/file-status"]')).toBeNull();
    expect(screen.queryByText('nav.fileStatus')).toBeNull();
    expect(screen.queryByText('nav.allFiles')).toBeNull();
  });
});
