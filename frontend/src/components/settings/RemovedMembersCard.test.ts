/**
 * The removed-members card (#1104): it lists accounts waiting out their grace period, restores by
 * the ledger uuid and drops the row only after the server accepted, re-reads the list on a 409
 * (the grace is over), and saves the grace period through the settings route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, params: Record<string, unknown> = {}) =>
    (en[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_, name) => String(params[name] ?? ''));
  return { t: readable(t), locale: readable('en') };
});

const api = vi.hoisted(() => ({
  getSelfErasureSettings: vi.fn(),
  putSelfErasureSettings: vi.fn(),
  listRemovedMembers: vi.fn(),
  restoreRemovedMember: vi.fn(),
}));
vi.mock('$lib/api/selfErasure', () => api);

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import RemovedMembersCard from './RemovedMembersCard.svelte';

const settings = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  grace_days: 14,
  max_grace_days: 21,
  removed_member_grace_days: 14,
  ...over,
});
const member = (over: Record<string, unknown> = {}) => ({
  ledger_uuid: 'aaaaaaaa-0000-0000-0000-000000000001',
  subject_user_uuid: 'bbbbbbbb-0000-0000-0000-000000000001',
  display_name: 'Alice Member',
  requested_at: '2026-11-12T12:00:00Z',
  scheduled_for: '2026-11-26T12:00:00Z',
  status: 'scheduled',
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  api.getSelfErasureSettings.mockResolvedValue(settings());
});

describe('RemovedMembersCard', () => {
  it('lists each removed member with Restore', async () => {
    api.listRemovedMembers.mockResolvedValue([member()]);
    render(RemovedMembersCard);

    expect(await screen.findByTestId('removed-member-row')).toBeTruthy();
    expect(screen.getByText('Alice Member')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Restore' })).toBeTruthy();
  });

  it('puts the date inside one translated sentence, with no joined colon', async () => {
    api.listRemovedMembers.mockResolvedValue([member()]);
    render(RemovedMembersCard);

    const row = await screen.findByTestId('removed-member-row');
    const text = row.querySelector('.what .hint')?.textContent?.trim() ?? '';
    expect(text).toMatch(/^Erased at .*2026/);
    expect(text).not.toContain(': ');
  });

  it('shows the empty line when nobody is waiting', async () => {
    api.listRemovedMembers.mockResolvedValue([]);
    render(RemovedMembersCard);

    expect(await screen.findByTestId('removed-members-empty')).toBeTruthy();
  });

  it('restores by ledger uuid and drops the row after the server accepted', async () => {
    api.listRemovedMembers.mockResolvedValue([member()]);
    api.restoreRemovedMember.mockResolvedValue(member({ status: 'cancelled' }));
    render(RemovedMembersCard);

    await fireEvent.click(await screen.findByRole('button', { name: 'Restore' }));

    await waitFor(() => expect(screen.queryByTestId('removed-member-row')).toBeNull());
    expect(api.restoreRemovedMember).toHaveBeenCalledWith('aaaaaaaa-0000-0000-0000-000000000001');
    expect(toast.success).toHaveBeenCalled();
  });

  it('keeps the list and says it is too late on a 409', async () => {
    api.listRemovedMembers.mockResolvedValue([member()]);
    api.restoreRemovedMember.mockRejectedValue({ response: { status: 409 } });
    render(RemovedMembersCard);

    await fireEvent.click(await screen.findByRole('button', { name: 'Restore' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(api.listRemovedMembers).toHaveBeenCalledTimes(2);
  });

  it('saves the grace period through the settings route', async () => {
    api.listRemovedMembers.mockResolvedValue([]);
    api.putSelfErasureSettings.mockResolvedValue(settings({ removed_member_grace_days: 7 }));
    render(RemovedMembersCard);

    const input = (await screen.findByLabelText('Grace period (days)')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: '7' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save grace period' }));

    await waitFor(() =>
      expect(api.putSelfErasureSettings).toHaveBeenCalledWith({ removed_member_grace_days: 7 })
    );
  });
});
