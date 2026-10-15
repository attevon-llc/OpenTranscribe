import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';

const h = vi.hoisted(() => ({
  end: vi.fn(),
  revokeGrant: vi.fn(),
  calls: [] as string[],
  session: null as unknown as Writable<SessionState>,
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k) => k), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('$lib/api/supportAccess', () => ({
  SupportAccessApi: { revokeGrant: h.revokeGrant },
}));

interface SessionState {
  active: boolean;
  grantUuid: string;
  targetLabel: string;
  targetKind: 'organization' | 'personal';
  level: 'read' | 'write';
  mode: 'approved' | 'break_glass';
  expiresAtMs: number;
  skewMs: number;
  remainingSeconds: number;
}

const INITIAL: SessionState = vi.hoisted(() => ({
  active: true,
  grantUuid: 'g-1',
  targetLabel: 'Acme',
  targetKind: 'organization',
  level: 'read',
  mode: 'approved',
  expiresAtMs: Date.parse('2026-10-09T13:00:00Z'),
  skewMs: 0,
  remainingSeconds: 1800,
}));

vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  const store = writable<SessionState>({ ...INITIAL });
  h.session = store;
  return {
    supportSession: {
      subscribe: store.subscribe,
      end: (reason: string) => {
        h.calls.push(`end:${reason}`);
        return h.end(reason);
      },
    },
  };
});

import SupportAccessBanner from './SupportAccessBanner.svelte';

// `bind:clientHeight` observes size changes; jsdom has no ResizeObserver.
vi.stubGlobal(
  'ResizeObserver',
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
);

function set(over: Partial<SessionState>) {
  h.session.update((s) => ({ ...s, ...over }));
}

beforeEach(() => {
  vi.clearAllMocks();
  h.calls.length = 0;
  h.revokeGrant.mockImplementation(async () => {
    h.calls.push('revoke');
  });
  h.session.set({ ...INITIAL });
});

const announcer = () => screen.getByTestId('support-announcer');

describe('SupportAccessBanner', () => {
  it('names the tenant and the access level', () => {
    render(SupportAccessBanner);
    expect(screen.getByRole('region', { name: 'supportAccess.banner.region' })).toBeTruthy();
    expect(screen.getByText('supportAccess.banner.org')).toBeTruthy();
    expect(screen.getByText('supportAccess.level.read')).toBeTruthy();
  });

  it('uses the personal-workspace wording for a personal grant', () => {
    set({ targetKind: 'personal' });
    render(SupportAccessBanner);
    expect(screen.getByText('supportAccess.banner.personal')).toBeTruthy();
    expect(screen.queryByText('supportAccess.banner.org')).toBeNull();
  });

  it('shows the Break-glass chip only for break-glass grants', () => {
    const { unmount } = render(SupportAccessBanner);
    expect(screen.queryByText('supportAccess.mode.break_glass')).toBeNull();
    unmount();
    set({ mode: 'break_glass' });
    render(SupportAccessBanner);
    expect(screen.getByText('supportAccess.mode.break_glass')).toBeTruthy();
  });

  it('announces the start, then 5 minutes at 300 s and 1 minute at 60 s, not before', async () => {
    set({ remainingSeconds: 301 });
    render(SupportAccessBanner);
    await waitFor(() => expect(announcer().textContent).toBe('supportAccess.announce.started'));

    set({ remainingSeconds: 300 });
    await waitFor(() => expect(announcer().textContent).toBe('supportAccess.announce.fiveMinutes'));

    set({ remainingSeconds: 61 });
    await waitFor(() => expect(announcer().textContent).toBe('supportAccess.announce.fiveMinutes'));

    set({ remainingSeconds: 60 });
    await waitFor(() => expect(announcer().textContent).toBe('supportAccess.announce.oneMinute'));
  });

  it('does not re-announce on the seconds after a threshold', async () => {
    set({ remainingSeconds: 301 });
    render(SupportAccessBanner);
    const seen: string[] = [];
    new MutationObserver(() => seen.push(announcer().textContent ?? '')).observe(announcer(), {
      childList: true,
      characterData: true,
      subtree: true,
    });
    set({ remainingSeconds: 300 });
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    const afterThreshold = seen.length;
    set({ remainingSeconds: 299 });
    set({ remainingSeconds: 298 });
    await new Promise((r) => setTimeout(r, 20));
    expect(seen.length).toBe(afterThreshold);
  });

  it('uses an assertive live region for break-glass and a polite one otherwise', () => {
    const { unmount } = render(SupportAccessBanner);
    expect(announcer().getAttribute('aria-live')).toBe('polite');
    unmount();
    set({ mode: 'break_glass' });
    render(SupportAccessBanner);
    expect(announcer().getAttribute('aria-live')).toBe('assertive');
  });

  it('End session ends the session as a user action without revoking the grant', async () => {
    render(SupportAccessBanner);
    await fireEvent.click(screen.getByRole('button', { name: 'supportAccess.action.endSession' }));
    expect(h.end).toHaveBeenCalledWith('user');
    expect(h.revokeGrant).not.toHaveBeenCalled();
  });

  it('Revoke access asks first, then revokes, then ends the session (in that order)', async () => {
    render(SupportAccessBanner);
    await fireEvent.click(screen.getByRole('button', { name: 'supportAccess.action.revoke' }));
    expect(h.revokeGrant).not.toHaveBeenCalled();
    expect(h.end).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'modal.confirm' }));
    await waitFor(() => expect(h.end).toHaveBeenCalledWith('revoked'));
    expect(h.revokeGrant).toHaveBeenCalledWith('g-1', {});
    expect(h.calls).toEqual(['revoke', 'end:revoked']);
  });

  it('keeps the session when the revoke call fails', async () => {
    h.revokeGrant.mockRejectedValueOnce(new Error('boom'));
    render(SupportAccessBanner);
    await fireEvent.click(screen.getByRole('button', { name: 'supportAccess.action.revoke' }));
    await fireEvent.click(screen.getByRole('button', { name: 'modal.confirm' }));
    await waitFor(() => expect(h.revokeGrant).toHaveBeenCalled());
    expect(h.end).not.toHaveBeenCalled();
  });
});
