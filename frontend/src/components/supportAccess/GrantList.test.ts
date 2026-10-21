import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  // Identity translator: assertions match on the i18n key.
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k, o) => (o && 'name' in o ? `${k}:${o.name}` : k)), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import GrantList, { rowActions } from './GrantList.svelte';
import type { GrantStatus, SupportGrant } from '$lib/api/supportAccess';

function grant(over: Partial<SupportGrant> = {}): SupportGrant {
  return {
    uuid: 'g1',
    status: 'active',
    target_kind: 'organization',
    grant_mode: 'approved',
    access_level: 'read',
    organization: { uuid: 'o1', name: 'Acme', slug: 'acme' },
    subject_user: null,
    grantee: { uuid: 'u1', full_name: 'Sam Support', email: 'sam@example.com' },
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

describe('rowActions (per-status matrix)', () => {
  const STATUSES: GrantStatus[] = ['pending', 'active', 'denied', 'expired', 'revoked', 'lapsed'];

  it.each([
    ['pending', ['revoke', 'viewLog']],
    ['active', ['start', 'revoke', 'viewLog']],
    ['denied', ['viewLog']],
    ['expired', ['viewLog']],
    ['revoked', ['viewLog']],
    ['lapsed', ['viewLog']],
  ])('staff / %s', (status, expected) => {
    expect(rowActions(status as GrantStatus, 'staff', false)).toEqual(expected);
  });

  it('staff / active and already in use shows "in use" instead of Start session', () => {
    expect(rowActions('active', 'staff', true)).toEqual(['inUse', 'revoke', 'viewLog']);
  });

  it.each([
    ['pending', ['approve', 'deny']],
    ['active', ['revoke', 'viewLog']],
    ['denied', ['viewLog']],
    ['expired', ['viewLog']],
    ['revoked', ['viewLog']],
    ['lapsed', ['viewLog']],
  ])('approver / %s', (status, expected) => {
    expect(rowActions(status as GrantStatus, 'approver', false)).toEqual(expected);
  });

  it('covers every status for both perspectives', () => {
    for (const s of STATUSES) {
      expect(rowActions(s, 'staff', false).length).toBeGreaterThan(0);
      expect(rowActions(s, 'approver', false).length).toBeGreaterThan(0);
    }
  });
});

describe('GrantList rendering', () => {
  it('shows "in use" and no Start button for the live session grant', () => {
    render(GrantList, { grants: [grant()], perspective: 'staff', activeGrantUuid: 'g1' });
    expect(screen.getByText('supportAccess.status.inUse')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /supportAccess.action.activate/ })).toBeNull();
  });

  it('offers Start session on an active grant that is not in use', () => {
    render(GrantList, { grants: [grant()], perspective: 'staff', activeGrantUuid: null });
    expect(screen.getByRole('button', { name: 'supportAccess.action.activate Acme' })).toBeTruthy();
  });

  it('names the target in each action label so repeated buttons are distinguishable', () => {
    render(GrantList, {
      grants: [
        grant({ uuid: 'a' }),
        grant({ uuid: 'b', organization: { uuid: 'o2', name: 'Globex', slug: null } }),
      ],
      perspective: 'staff',
    });
    expect(screen.getByRole('button', { name: 'supportAccess.action.revoke Acme' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'supportAccess.action.revoke Globex' })).toBeTruthy();
  });

  it('renders a deleted grantee explicitly for an approver', () => {
    render(GrantList, {
      grants: [grant({ grantee: null, status: 'pending' })],
      perspective: 'approver',
    });
    expect(screen.getByText('supportAccess.deletedUser')).toBeTruthy();
  });

  it('renders a deleted organization explicitly for staff', () => {
    render(GrantList, { grants: [grant({ organization: null })], perspective: 'staff' });
    expect(screen.getByText('supportAccess.deletedOrganization')).toBeTruthy();
  });

  it('shows the ticket reference on a break-glass row', () => {
    render(GrantList, {
      grants: [grant({ grant_mode: 'break_glass', ticket_ref: 'INC-4242' })],
      perspective: 'approver',
    });
    expect(screen.getByText('INC-4242')).toBeTruthy();
    expect(screen.getByText('supportAccess.mode.break_glass')).toBeTruthy();
  });

  it('dispatches the row grant when Approve is clicked', async () => {
    const pending = grant({ status: 'pending' });
    const handler = vi.fn();
    render(GrantList, {
      props: { grants: [pending], perspective: 'approver' },
      events: { approve: (e: CustomEvent) => handler(e.detail) },
    });
    await fireEvent.click(
      screen.getByRole('button', { name: 'supportAccess.action.approve Sam Support' })
    );
    expect(handler).toHaveBeenCalledWith(pending);
  });

  it('disables only the busy row’s buttons', () => {
    render(GrantList, {
      grants: [grant({ uuid: 'a' }), grant({ uuid: 'b' })],
      perspective: 'staff',
      busyUuid: 'a',
    });
    const buttons = screen.getAllByRole('button', { name: /supportAccess.action.revoke/ });
    expect((buttons[0] as HTMLButtonElement).disabled).toBe(true);
    expect((buttons[1] as HTMLButtonElement).disabled).toBe(false);
  });
});
