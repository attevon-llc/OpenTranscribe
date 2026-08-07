import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return {
    axiosInstance,
    default: axiosInstance,
    isRequestCancelled: () => false,
  };
});

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('$stores/auth', () => ({
  logout: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('$stores/locale', () => ({
  // Identity translator so assertions can match on the i18n key.
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import axiosInstance from '$lib/axios';
import { logout } from '$stores/auth';
import { toastStore } from '$stores/toast';
import ActiveSessionsPanel from './ActiveSessionsPanel.svelte';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);

// jsdom refuses real navigation; stub the one method the panel calls so the
// redirect is observable instead of a console error.
const assign = vi.fn();

function session(overrides: Record<string, unknown> = {}) {
  return {
    jti: 'jti-1',
    created_at: new Date(Date.now() - 3600_000).toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    user_agent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36',
    ip_address: '10.0.0.7',
    ...overrides,
  };
}

describe('ActiveSessionsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { href: 'http://localhost/', assign },
    });
  });

  it('renders an empty state rather than crashing on an empty session list', async () => {
    get.mockResolvedValue({ data: { sessions: [], total: 0 } } as never);

    render(ActiveSessionsPanel);

    await waitFor(() =>
      expect(screen.getByText('settings.sessions.empty')).toBeInTheDocument()
    );
    expect(screen.getByText('settings.sessions.emptyDescription')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    // Nothing to sign out of, so the destructive action is unavailable.
    expect(
      screen.getByRole('button', { name: 'settings.sessions.signOutEverywhere' })
    ).toBeDisabled();
  });

  it('survives a response with no sessions key at all', async () => {
    get.mockResolvedValue({ data: {} } as never);

    render(ActiveSessionsPanel);

    await waitFor(() =>
      expect(screen.getByText('settings.sessions.empty')).toBeInTheDocument()
    );
    expect(toastStore.error).not.toHaveBeenCalled();
  });

  it('lists sessions with a humanised device label and the IP', async () => {
    get.mockResolvedValue({ data: { sessions: [session()], total: 1 } } as never);

    render(ActiveSessionsPanel);

    await waitFor(() => expect(screen.getByText('Chrome — Windows')).toBeInTheDocument());
    expect(screen.getByText('10.0.0.7')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/auth/sessions');
  });

  it('falls back to a label instead of blanking an unknown user agent or IP', async () => {
    get.mockResolvedValue({
      data: { sessions: [session({ user_agent: null, ip_address: null })], total: 1 },
    } as never);

    render(ActiveSessionsPanel);

    await waitFor(() =>
      expect(screen.getByText('settings.sessions.unknownDevice')).toBeInTheDocument()
    );
    expect(screen.getByText('settings.sessions.unknownIp')).toBeInTheDocument();
  });

  it('signs out everywhere only after confirmation, then tears the client session down', async () => {
    get.mockResolvedValue({ data: { sessions: [session()], total: 1 } } as never);
    post.mockResolvedValue({ data: { sessions_revoked: 1 } } as never);

    render(ActiveSessionsPanel);

    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'settings.sessions.signOutEverywhere' })
      ).toBeEnabled()
    );
    await fireEvent.click(
      screen.getByRole('button', { name: 'settings.sessions.signOutEverywhere' })
    );

    // The confirmation modal is up; nothing has been revoked yet.
    expect(post).not.toHaveBeenCalled();

    const confirmButtons = screen.getAllByRole('button', {
      name: 'settings.sessions.signOutEverywhere',
    });
    await fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => expect(post).toHaveBeenCalledWith('/auth/logout/all'));
    // POST /auth/logout/all also clears this browser's cookies, so the client
    // state must be torn down and the user sent back to the login page.
    await waitFor(() => expect(logout).toHaveBeenCalled());
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/login'));
  });

  it('reports a load failure instead of rendering a half-populated table', async () => {
    get.mockRejectedValue({ response: { data: { detail: 'nope' } } } as never);

    render(ActiveSessionsPanel);

    await waitFor(() => expect(toastStore.error).toHaveBeenCalledWith('nope'));
    expect(screen.getByText('settings.sessions.empty')).toBeInTheDocument();
  });
});
