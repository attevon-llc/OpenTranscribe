/**
 * IdleTimeoutDialog (issue #1106): what the user sees for each guard state, and
 * that the lock screen conceals the page and offers no way back in.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';

type LockState =
  | { phase: 'active' }
  | { phase: 'warning'; reason: 'idle' | 'absolute'; deadline: number }
  | { phase: 'locked'; reason: 'idle' | 'absolute'; waitingForUploads: boolean };

const timeouts = vi.hoisted(() => ({
  sessionLock: null as unknown as import('svelte/store').Writable<LockState>,
  loadSessionTimeoutConfig: vi.fn(async () => ({
    idleMinutes: 15,
    absoluteMinutes: 480,
    authTimeSeconds: null,
  })),
  startSessionTimeouts: vi.fn((_config: unknown, _deps: unknown) => true),
  stopSessionTimeouts: vi.fn(),
  staySignedIn: vi.fn(),
  endExpiredSession: vi.fn(async () => {}),
}));
timeouts.sessionLock = writable<LockState>({ phase: 'active' });

vi.mock('$lib/auth/sessionTimeouts', () => timeouts);
vi.mock('$stores/uploads', () => ({ hasActiveUploads: writable(false) }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key));
      return () => {};
    },
  },
}));

import IdleTimeoutDialog from './IdleTimeoutDialog.svelte';

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
  timeouts.sessionLock.set({ phase: 'active' });
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('IdleTimeoutDialog', () => {
  it('renders nothing while the session is active', () => {
    const { container } = render(IdleTimeoutDialog);
    expect(container.querySelector('[role="alertdialog"]')).toBeNull();
  });

  it('starts the guard with the loaded config and stops it on unmount', async () => {
    const { unmount } = render(IdleTimeoutDialog);
    await vi.waitFor(() => expect(timeouts.startSessionTimeouts).toHaveBeenCalledTimes(1));
    expect(timeouts.startSessionTimeouts.mock.calls[0][0]).toEqual({
      idleMinutes: 15,
      absoluteMinutes: 480,
      authTimeSeconds: null,
    });
    unmount();
    expect(timeouts.stopSessionTimeouts).toHaveBeenCalled();
  });

  it('shows a countdown and "stay signed in" for an idle warning', async () => {
    render(IdleTimeoutDialog);
    timeouts.sessionLock.set({ phase: 'warning', reason: 'idle', deadline: Date.now() + 90_000 });
    await Promise.resolve();

    const dialog = await screen.findByRole('alertdialog');
    expect(dialog.textContent).toContain('auth.sessionTimeout.idleWarning {"time":"1:30"}');

    await fireEvent.click(screen.getByText('auth.sessionTimeout.staySignedIn'));
    expect(timeouts.staySignedIn).toHaveBeenCalledTimes(1);
  });

  it('the countdown ticks', async () => {
    render(IdleTimeoutDialog);
    timeouts.sessionLock.set({ phase: 'warning', reason: 'idle', deadline: Date.now() + 90_000 });
    await screen.findByRole('alertdialog');
    await vi.advanceTimersByTimeAsync(31_000);
    expect(screen.getByRole('alertdialog').textContent).toContain('{"time":"0:59"}');
  });

  it('offers no "stay signed in" for the absolute limit', async () => {
    render(IdleTimeoutDialog);
    timeouts.sessionLock.set({
      phase: 'warning',
      reason: 'absolute',
      deadline: Date.now() + 60_000,
    });
    await screen.findByRole('alertdialog');

    expect(screen.queryByText('auth.sessionTimeout.staySignedIn')).toBeNull();
    await fireEvent.click(screen.getByText('auth.sessionTimeout.signInAgain'));
    expect(timeouts.endExpiredSession).toHaveBeenCalledWith('absolute');
  });

  it('locks with an opaque screen and no way back in', async () => {
    render(IdleTimeoutDialog);
    timeouts.sessionLock.set({ phase: 'locked', reason: 'idle', waitingForUploads: false });

    const lock = await screen.findByTestId('session-lock');
    expect(lock.textContent).toContain('auth.sessionTimeout.lockedIdle');
    expect(screen.queryByText('auth.sessionTimeout.staySignedIn')).toBeNull();
    expect(lock.querySelectorAll('button')).toHaveLength(0);
  });

  it('while uploads finish, explains the wait and allows signing out now', async () => {
    render(IdleTimeoutDialog);
    timeouts.sessionLock.set({ phase: 'locked', reason: 'absolute', waitingForUploads: true });

    const lock = await screen.findByTestId('session-lock');
    expect(lock.textContent).toContain('auth.sessionTimeout.lockedAbsolute');
    expect(lock.textContent).toContain('auth.sessionTimeout.waitingForUploads');
    await fireEvent.click(screen.getByText('auth.sessionTimeout.signOutNow'));
    expect(timeouts.endExpiredSession).toHaveBeenCalledWith('absolute');
  });
});
