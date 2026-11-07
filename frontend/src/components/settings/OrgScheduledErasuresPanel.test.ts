/**
 * The scheduled-erasures panel (#1105): it renders what the server lists, cancels by the ledger
 * uuid, drops the row only after the server accepted, and re-reads the list when the server says
 * the erasure already started.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string) => en[key] ?? key;
  return { t: readable(t), locale: readable('en') };
});

const api = vi.hoisted(() => ({ listScheduledErasures: vi.fn(), cancelScheduledErasure: vi.fn() }));
vi.mock('$lib/api/orgAdmin', () => ({ OrgAdminApi: api }));

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import OrgScheduledErasuresPanel from './OrgScheduledErasuresPanel.svelte';

const item = (over: Record<string, unknown> = {}) => ({
  ledger_uuid: 'aaaaaaaa-0000-0000-0000-000000000001',
  subject_type: 'organization',
  subject_user_uuid: null,
  requested_at: '2026-11-07T12:00:00Z',
  scheduled_for: '2026-11-10T12:00:00Z',
  sla_due_at: '2026-12-07T12:00:00Z',
  status: 'scheduled',
  cancelled_at: null,
  requested_by: 'Alice Owner',
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('OrgScheduledErasuresPanel', () => {
  it('lists each open erasure with who asked and offers Cancel', async () => {
    api.listScheduledErasures.mockResolvedValue([
      item(),
      item({ ledger_uuid: 'b', subject_type: 'org_member', requested_by: null }),
    ]);
    render(OrgScheduledErasuresPanel);

    const rows = await screen.findAllByTestId('org-erasure-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('Whole organization');
    expect(rows[0].textContent).toContain('Alice Owner');
    expect(rows[1].textContent).toContain("One member's organization data");
    expect(rows[1].textContent).toContain('A former member');
    expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(2);
  });

  it('says so when nothing is scheduled', async () => {
    api.listScheduledErasures.mockResolvedValue([]);
    render(OrgScheduledErasuresPanel);

    expect(await screen.findByText('No erasure is scheduled')).toBeTruthy();
    expect(screen.queryAllByRole('button', { name: 'Cancel' })).toHaveLength(0);
  });

  it('cancels by uuid and removes the row only after the server accepted', async () => {
    api.listScheduledErasures.mockResolvedValue([item()]);
    api.cancelScheduledErasure.mockResolvedValue(item({ status: 'cancelled' }));
    render(OrgScheduledErasuresPanel);

    await fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryAllByTestId('org-erasure-row')).toHaveLength(0));
    expect(api.cancelScheduledErasure).toHaveBeenCalledWith('aaaaaaaa-0000-0000-0000-000000000001');
    expect(toast.success).toHaveBeenCalledWith('The erasure was cancelled.');
  });

  it('keeps the row and re-reads the list when the erasure already started (409)', async () => {
    api.listScheduledErasures.mockResolvedValue([item()]);
    api.cancelScheduledErasure.mockRejectedValue({ response: { status: 409 } });
    render(OrgScheduledErasuresPanel);

    await fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(api.listScheduledErasures).toHaveBeenCalledTimes(2));
    expect(toast.error).toHaveBeenCalledWith(
      'It has already started and can no longer be cancelled.'
    );
  });

  it('shows a retry when the list cannot be loaded', async () => {
    api.listScheduledErasures.mockRejectedValueOnce(new Error('boom'));
    render(OrgScheduledErasuresPanel);

    expect((await screen.findByRole('alert')).textContent).toContain('could not be loaded');
    api.listScheduledErasures.mockResolvedValue([item()]);
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findAllByTestId('org-erasure-row')).toHaveLength(1);
  });
});
