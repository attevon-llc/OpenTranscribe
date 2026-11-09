/**
 * The navbar logo opens the About (credits) modal, including in the cloud build.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

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
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/settingsModalStore', () => ({ settingsModalStore: { open: vi.fn() } }));
vi.mock('$lib/prefetch', () => ({
  prefetchSpeakersData: vi.fn(),
}));
vi.mock('$lib/edition', () => ({ isCloudEdition: true }));
vi.mock('$lib/cloud', async () => (await import('$testMocks/navbarMocks')).cloudMock());
vi.mock('$components/navbar/UserDropdown.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));

import Navbar from './Navbar.svelte';
import { supportGate } from '$testMocks/navbarMocks';
import { capabilities } from '$stores/capabilities';

beforeEach(() => {
  capabilities.set({
    edition: 'cloud',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  supportGate.set({ active: false, readOnly: false });
});

describe('Navbar logo opens the About modal (cloud edition build)', () => {
  it('shows the credits dialog on the first click', async () => {
    const { container } = render(Navbar);
    await fireEvent.click(container.querySelector('button.logo-link')!);
    expect(await screen.findByText('about.title')).toBeTruthy();
  });
});
