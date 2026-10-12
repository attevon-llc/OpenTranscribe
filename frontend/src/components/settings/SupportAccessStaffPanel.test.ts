import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const h = vi.hoisted(() => ({
  listMyGrants: vi.fn(),
  revokeGrant: vi.fn(),
  activate: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k) => k), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));
vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ role: string } | null>({ role: 'admin' }) };
});
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('$stores/supportSession', async () => {
  const { readable } = await import('svelte/store');
  const store = readable({ grantUuid: null as string | null });
  return {
    supportSession: { subscribe: store.subscribe, activate: h.activate, end: vi.fn() },
  };
});
vi.mock('$lib/api/supportAccess', () => ({
  SupportAccessApi: {
    listMyGrants: h.listMyGrants,
    revokeGrant: h.revokeGrant,
    searchOrganizations: vi.fn().mockResolvedValue([]),
    listUses: vi.fn().mockResolvedValue({ items: [], total: 0, server_time: 'x' }),
  },
}));
vi.mock('$lib/api/admin', () => ({ AdminApi: { searchUsers: vi.fn() } }));

import SupportAccessStaffPanel from './SupportAccessStaffPanel.svelte';
import { user as mockUser } from '$stores/auth';
import type { SupportGrant } from '$lib/api/supportAccess';

function setRole(role: 'admin' | 'super_admin') {
  (mockUser as unknown as { set: (v: { role: string }) => void }).set({ role });
}

function grant(over: Partial<SupportGrant> = {}): SupportGrant {
  return {
    uuid: 'g1',
    status: 'active',
    target_kind: 'organization',
    grant_mode: 'approved',
    access_level: 'read',
    organization: { uuid: 'o1', name: 'Acme', slug: 'acme' },
    subject_user: null,
    grantee: { uuid: 'u1', full_name: 'Sam', email: 'sam@example.com' },
    reason: 'Customer reported a stuck transcript',
    ticket_ref: null,
    requested_duration_minutes: 60,
    requested_at: '2026-10-09T12:00:00Z',
    pending_expires_at: null,
    decided_by: null,
    decided_at: null,
    starts_at: null,
    expires_at: '2026-10-09T13:00:00Z',
    revoked_by: null,
    revoked_at: null,
    ...over,
  };
}

const page = (items: SupportGrant[]) => ({ items, total: items.length, server_time: 'x' });

beforeEach(() => {
  vi.clearAllMocks();
  setRole('admin');
  h.listMyGrants.mockResolvedValue(page([grant()]));
  h.revokeGrant.mockResolvedValue(grant({ status: 'revoked' }));
  h.activate.mockResolvedValue(true);
});

afterEach(() => vi.useRealTimers());

describe('SupportAccessStaffPanel', () => {
  it('loads the caller’s own grants first', async () => {
    render(SupportAccessStaffPanel);
    await screen.findByText('Acme');
    expect(h.listMyGrants).toHaveBeenCalledWith({ scope: 'mine', limit: 25, offset: 0 });
  });

  it('a plain admin gets a disabled, explained Break glass button and no tabs', async () => {
    render(SupportAccessStaffPanel);
    await screen.findByText('Acme');
    const bg = screen.getByRole('button', { name: /supportAccess.action.breakGlass/ });
    expect((bg as HTMLButtonElement).disabled).toBe(true);
    expect(bg.parentElement?.getAttribute('title')).toBe('supportAccess.breakGlass.superAdminOnly');
    expect(bg.getAttribute('aria-describedby')).toBe('bg-locked-reason');
    expect(screen.queryByRole('tab', { name: 'supportAccess.tabs.all' })).toBeNull();
  });

  it('a super admin can break glass and can switch to every grant (scope=all)', async () => {
    setRole('super_admin');
    render(SupportAccessStaffPanel);
    await screen.findByText('Acme');
    const bg = screen.getByRole('button', { name: /supportAccess.action.breakGlass/ });
    expect((bg as HTMLButtonElement).disabled).toBe(false);

    await fireEvent.click(screen.getByRole('tab', { name: 'supportAccess.tabs.all' }));
    await waitFor(() =>
      expect(h.listMyGrants).toHaveBeenLastCalledWith({ scope: 'all', limit: 25, offset: 0 })
    );
  });

  it('shows "no grants" and "could not load" as different states', async () => {
    h.listMyGrants.mockResolvedValueOnce(page([]));
    const empty = render(SupportAccessStaffPanel);
    expect(await screen.findByText('supportAccess.empty.staff')).toBeTruthy();
    expect(screen.queryByText('supportAccess.loadFailed')).toBeNull();
    empty.unmount();

    h.listMyGrants.mockRejectedValueOnce(new Error('500'));
    render(SupportAccessStaffPanel);
    expect(await screen.findByText('supportAccess.loadFailed')).toBeTruthy();
    expect(screen.queryByText('supportAccess.empty.staff')).toBeNull();
  });

  it('Start session activates exactly that grant', async () => {
    render(SupportAccessStaffPanel);
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.activate Acme' })
    );
    expect(h.activate).toHaveBeenCalledWith('g1');
  });

  it('Revoke opens the note dialog and posts the note, then reloads', async () => {
    render(SupportAccessStaffPanel);
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.revoke Acme' })
    );
    await fireEvent.input(screen.getByLabelText('supportAccess.note.label'), {
      target: { value: 'finished' },
    });
    const callsBefore = h.listMyGrants.mock.calls.length;
    await fireEvent.click(screen.getByRole('button', { name: 'supportAccess.action.revoke' }));

    await waitFor(() => expect(h.revokeGrant).toHaveBeenCalledWith('g1', { note: 'finished' }));
    await waitFor(() => expect(h.listMyGrants.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it('polls every 30 s so a pending request turns active without a reload', async () => {
    vi.useFakeTimers();
    render(SupportAccessStaffPanel);
    await vi.advanceTimersByTimeAsync(0);
    const initial = h.listMyGrants.mock.calls.length;
    await vi.advanceTimersByTimeAsync(29_000);
    expect(h.listMyGrants.mock.calls.length).toBe(initial);
    await vi.advanceTimersByTimeAsync(1_500);
    expect(h.listMyGrants.mock.calls.length).toBe(initial + 1);
  });

  it('offers Open file by id only when an active personal-workspace grant exists', async () => {
    h.listMyGrants.mockResolvedValue(page([grant()]));
    const org = render(SupportAccessStaffPanel);
    await screen.findByText('Acme');
    expect(screen.queryByLabelText('supportAccess.openFile.label')).toBeNull();
    org.unmount();

    h.listMyGrants.mockResolvedValue(
      page([
        grant({
          target_kind: 'personal',
          organization: null,
          subject_user: { uuid: 'u2', full_name: 'Jane', email: 'j@example.com' },
        }),
      ])
    );
    render(SupportAccessStaffPanel);
    expect(await screen.findByLabelText('supportAccess.openFile.label')).toBeTruthy();
  });
});
