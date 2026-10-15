import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const h = vi.hoisted(() => ({
  listRequests: vi.fn(),
  approve: vi.fn(),
  deny: vi.fn(),
  revokeGrant: vi.fn(),
  listUses: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k) => k), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));
vi.mock('$stores/toast', () => ({ toastStore: { error: h.toastError, success: h.toastSuccess } }));
vi.mock('$lib/api/supportAccess', () => ({
  SupportAccessApi: {
    listRequests: h.listRequests,
    approve: h.approve,
    deny: h.deny,
    revokeGrant: h.revokeGrant,
    listUses: h.listUses,
  },
}));

import SupportAccessApprovalsPanel from './SupportAccessApprovalsPanel.svelte';
import type { GrantStatus, SupportGrant } from '$lib/api/supportAccess';

function grant(uuid: string, status: GrantStatus, over: Partial<SupportGrant> = {}): SupportGrant {
  return {
    uuid,
    status,
    target_kind: 'personal',
    grant_mode: 'approved',
    access_level: 'read',
    organization: null,
    subject_user: { uuid: 'me', full_name: 'Me', email: 'me@example.com' },
    grantee: { uuid: 'staff', full_name: 'Sam Support', email: 'sam@example.com' },
    reason: 'Customer reported a stuck transcript',
    ticket_ref: null,
    requested_duration_minutes: 60,
    requested_at: '2026-10-09T12:00:00Z',
    pending_expires_at: null,
    decided_by: null,
    decided_at: null,
    starts_at: null,
    expires_at: null,
    revoked_by: null,
    revoked_at: null,
    ...over,
  };
}

const page = (items: SupportGrant[]) => ({ items, total: items.length, server_time: 'x' });

function wireList(pending: SupportGrant[], history: SupportGrant[] = []) {
  h.listRequests.mockImplementation(
    async (_perspective: string, params: { status?: GrantStatus }) =>
      params.status === 'pending' ? page(pending) : page([...pending, ...history])
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  h.approve.mockResolvedValue(grant('g1', 'active'));
  h.deny.mockResolvedValue(grant('g1', 'denied'));
  h.revokeGrant.mockResolvedValue(grant('g1', 'revoked'));
  h.listUses.mockResolvedValue({ items: [], total: 0, server_time: 'x' });
  wireList([grant('g1', 'pending')]);
});

const pendingCalls = () =>
  h.listRequests.mock.calls.filter((c) => (c[1] as { status?: string }).status === 'pending');

describe('SupportAccessApprovalsPanel', () => {
  it('has no Organization tab for someone who is not an organization admin', async () => {
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    await screen.findByText('Sam Support');
    expect(screen.queryByRole('tab', { name: 'supportAccess.tabs.organization' })).toBeNull();
    expect(h.listRequests).toHaveBeenCalledWith('workspace', expect.anything());
    expect(h.listRequests).not.toHaveBeenCalledWith('org', expect.anything());
  });

  it('an organization admin gets the tab, and it reads the organization routes', async () => {
    render(SupportAccessApprovalsPanel, { props: { orgTab: true } });
    await screen.findByText('Sam Support');
    await fireEvent.click(screen.getByRole('tab', { name: 'supportAccess.tabs.organization' }));
    await waitFor(() => expect(h.listRequests).toHaveBeenCalledWith('org', expect.anything()));
  });

  it('lists pending requests and the (non-pending) history separately', async () => {
    wireList(
      [grant('g1', 'pending')],
      [grant('h1', 'denied', { reason: 'An old, denied request' })]
    );
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    expect(
      await screen.findByRole('heading', { name: /supportAccess.section.pending/ })
    ).toBeTruthy();
    expect(await screen.findByText('An old, denied request')).toBeTruthy();
    // The pending row must appear once: the history list hides pending rows.
    expect(screen.getAllByText('Customer reported a stuck transcript')).toHaveLength(1);
  });

  it('approves with the shortened duration through the right perspective', async () => {
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.approve Sam Support' })
    );
    await fireEvent.change(screen.getByLabelText('supportAccess.approve.grantFor'), {
      target: { value: '30' },
    });
    const dialogApprove = screen
      .getAllByRole('button', { name: 'supportAccess.action.approve' })
      .at(-1) as HTMLElement;
    await fireEvent.click(dialogApprove);

    await waitFor(() =>
      expect(h.approve).toHaveBeenCalledWith('workspace', 'g1', { duration_minutes: 30 })
    );
    expect(h.toastSuccess).toHaveBeenCalledWith('supportAccess.approve.approved');
  });

  it('a 409 already_decided toasts, reloads, and never retries the decision', async () => {
    h.approve.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { code: 'support_grant_already_decided', message: 'decided' } },
      },
    });
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.approve Sam Support' })
    );
    const before = pendingCalls().length;
    await fireEvent.click(
      screen.getAllByRole('button', { name: 'supportAccess.action.approve' }).at(-1) as HTMLElement
    );

    await waitFor(() => expect(h.toastError).toHaveBeenCalledWith('supportAccess.alreadyDecided'));
    await waitFor(() => expect(pendingCalls().length).toBeGreaterThan(before));
    expect(h.approve).toHaveBeenCalledTimes(1);
  });

  it('shows the self-approval message for a self_approval refusal', async () => {
    h.approve.mockRejectedValue({
      response: {
        status: 403,
        data: { detail: { code: 'support_grant_self_approval', message: 'no' } },
      },
    });
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.approve Sam Support' })
    );
    await fireEvent.click(
      screen.getAllByRole('button', { name: 'supportAccess.action.approve' }).at(-1) as HTMLElement
    );
    await waitFor(() => expect(h.toastError).toHaveBeenCalledWith('supportAccess.selfApproval'));
  });

  it('denies with an optional note', async () => {
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.deny Sam Support' })
    );
    await fireEvent.input(screen.getByLabelText('supportAccess.note.label'), {
      target: { value: 'not needed' },
    });
    await fireEvent.click(
      screen.getAllByRole('button', { name: 'supportAccess.action.deny' }).at(-1) as HTMLElement
    );
    await waitFor(() =>
      expect(h.deny).toHaveBeenCalledWith('workspace', 'g1', { note: 'not needed' })
    );
  });

  it('revokes an active grant from the history list through the shared revoke route', async () => {
    wireList([], [grant('a1', 'active', { expires_at: '2026-10-09T13:00:00Z' })]);
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.revoke Sam Support' })
    );
    await fireEvent.click(
      screen.getAllByRole('button', { name: 'supportAccess.action.revoke' }).at(-1) as HTMLElement
    );
    await waitFor(() => expect(h.revokeGrant).toHaveBeenCalledWith('a1', {}));
  });

  it('opens the tenant’s access log for a non-pending row on that perspective’s route', async () => {
    wireList([], [grant('a1', 'expired')]);
    render(SupportAccessApprovalsPanel, { props: { orgTab: false } });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.action.viewLog Sam Support' })
    );
    await waitFor(() =>
      expect(h.listUses).toHaveBeenCalledWith('workspace', 'a1', { limit: 25, offset: 0 })
    );
  });
});
