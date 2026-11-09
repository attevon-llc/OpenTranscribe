/**
 * A failed About chunk load must tell the user and let the next click retry, never a silent no-op.
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
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('./AboutModal.svelte', () => {
  throw new Error('chunk load failed');
});
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
  supportGate.set({ active: false, readOnly: false });
});

describe('Navbar About chunk failure', () => {
  it('shows an error toast and resets so the click can retry', async () => {
    const { container } = render(Navbar);
    await fireEvent.click(container.querySelector('button.logo-link')!);
    await vi.waitFor(() => expect(toastStore.error).toHaveBeenCalledWith('nav.aboutLoadFailed'));
    expect(screen.queryByText('about.title')).toBeNull();
  });
});
