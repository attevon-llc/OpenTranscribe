import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k, o) => (o ? `${k} ${JSON.stringify(o)}` : k)), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import ApproveGrantModal from './ApproveGrantModal.svelte';
import type { SupportGrant } from '$lib/api/supportAccess';

function grant(requested: number): SupportGrant {
  return {
    uuid: 'g1',
    status: 'pending',
    target_kind: 'personal',
    grant_mode: 'approved',
    access_level: 'read',
    organization: null,
    subject_user: { uuid: 'u2', full_name: 'Jane', email: 'jane@example.com' },
    grantee: { uuid: 'u1', full_name: 'Sam Support', email: 'sam@example.com' },
    reason: 'Customer reported a stuck transcript',
    ticket_ref: null,
    requested_duration_minutes: requested,
    requested_at: '2026-10-09T12:00:00Z',
    pending_expires_at: null,
    decided_by: null,
    decided_at: null,
    starts_at: null,
    expires_at: null,
    revoked_by: null,
    revoked_at: null,
  };
}

const options = () =>
  Array.from(
    (screen.getByLabelText('supportAccess.approve.grantFor') as HTMLSelectElement).options
  ).map((o) => o.value);

describe('ApproveGrantModal', () => {
  it('offers only durations up to the request: a 60 minute request has no 120 option', () => {
    render(ApproveGrantModal, { props: { isOpen: true, grant: grant(60) } });
    expect(options()).toEqual(['15', '30', '60']);
    expect(options()).not.toContain('120');
  });

  it('includes an odd requested duration itself so approving "as asked" is possible', () => {
    render(ApproveGrantModal, { props: { isOpen: true, grant: grant(90) } });
    expect(options()).toEqual(['15', '30', '60', '90']);
  });

  it('defaults to the requested duration and submits it', async () => {
    const onSubmit = vi.fn();
    render(ApproveGrantModal, {
      props: { isOpen: true, grant: grant(120) },
      events: { submit: (e: CustomEvent) => onSubmit(e.detail) },
    });
    expect(
      (screen.getByLabelText('supportAccess.approve.grantFor') as HTMLSelectElement).value
    ).toBe('120');
    await fireEvent.click(screen.getByRole('button', { name: 'supportAccess.action.approve' }));
    expect(onSubmit).toHaveBeenCalledWith({ duration_minutes: 120 });
  });

  it('submits the shortened duration the approver picked', async () => {
    const onSubmit = vi.fn();
    render(ApproveGrantModal, {
      props: { isOpen: true, grant: grant(120) },
      events: { submit: (e: CustomEvent) => onSubmit(e.detail) },
    });
    await fireEvent.change(screen.getByLabelText('supportAccess.approve.grantFor'), {
      target: { value: '30' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'supportAccess.action.approve' }));
    expect(onSubmit).toHaveBeenCalledWith({ duration_minutes: 30 });
  });

  it('shows who asked, why, and the shorten-only rule', () => {
    render(ApproveGrantModal, { props: { isOpen: true, grant: grant(60) } });
    expect(screen.getByText('Sam Support')).toBeTruthy();
    expect(screen.getByText('Customer reported a stuck transcript')).toBeTruthy();
    expect(screen.getByText('supportAccess.approve.shortenOnly')).toBeTruthy();
  });
});
