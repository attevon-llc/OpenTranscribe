import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  // Identity translator so assertions can match on the i18n key.
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import QuarantinePanel from './QuarantinePanel.svelte';
import { toRows } from './quarantineRows';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);

const orgRow = (over: Record<string, unknown> = {}) => ({
  uuid: 'file-own',
  filename: 'own.wav',
  title: null,
  owner_uuid: 'o1',
  owner_display: 'Olga Owner',
  quarantined_at: '2026-10-21T10:00:00Z',
  legal_hold: false,
  is_quarantined: true,
  takedown_origin: 'organization',
  can_release: true,
  quarantine_reason: 'policy 4.2',
  quarantined_by_display: 'Ada Admin',
  ...over,
});

// A takedown the operator placed: the server omits the reason and actor entirely.
const operatorRow = () => {
  const row = orgRow({
    uuid: 'file-op',
    filename: 'op.wav',
    takedown_origin: 'platform',
    can_release: false,
  });
  delete (row as Record<string, unknown>).quarantine_reason;
  delete (row as Record<string, unknown>).quarantined_by_display;
  return row;
};

describe('QuarantinePanel, organization scope (issue #1253)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('reads only the organization endpoint, never the platform queue', async () => {
    get.mockResolvedValue({ data: { files: [orgRow()], total: 1 } } as never);

    render(QuarantinePanel, { scope: 'organization' });

    await waitFor(() => expect(screen.getByText('own.wav')).toBeInTheDocument());
    expect(get).toHaveBeenCalledTimes(1);
    expect(get.mock.calls[0][0]).toBe('/org-admin/files/quarantined');
    expect(screen.getByText('settings.quarantine.orgTitle')).toBeInTheDocument();
  });

  it('offers Release for the files the organization placed and shows who and why', async () => {
    get.mockResolvedValue({ data: { files: [orgRow()], total: 1 } } as never);

    render(QuarantinePanel, { scope: 'organization' });

    await waitFor(() => expect(screen.getByText('policy 4.2')).toBeInTheDocument());
    expect(screen.getByText('Ada Admin')).toBeInTheDocument();
    expect(screen.getByText('settings.quarantine.heldByOrganization')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'settings.quarantine.release' })).toBeEnabled();
  });

  it('shows an operator takedown without a Release button, a reason or an actor', async () => {
    get.mockResolvedValue({ data: { files: [operatorRow()], total: 1 } } as never);

    render(QuarantinePanel, { scope: 'organization' });

    await waitFor(() => expect(screen.getByText('op.wav')).toBeInTheDocument());
    expect(screen.getByText('settings.quarantine.heldByOperator')).toBeInTheDocument();
    expect(screen.getByText('settings.quarantine.operatorHeldReason')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'settings.quarantine.release' })
    ).not.toBeInTheDocument();
    expect(screen.getByText('settings.quarantine.operatorOnlyRelease')).toBeInTheDocument();
  });

  it('releases through the organization endpoint and refetches', async () => {
    get.mockResolvedValue({ data: { files: [orgRow()], total: 1 } } as never);
    post.mockResolvedValue({ data: { uuid: 'file-own', is_quarantined: false } } as never);

    render(QuarantinePanel, { scope: 'organization' });
    const button = await screen.findByRole('button', { name: 'settings.quarantine.release' });
    await fireEvent.click(button);

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][0]).toBe('/org-admin/files/file-own/release');
    await waitFor(() => expect(toastStore.success).toHaveBeenCalled());
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('refetches, and does not retry, when the release is refused', async () => {
    get.mockResolvedValue({ data: { files: [orgRow()], total: 1 } } as never);
    post.mockRejectedValue({
      response: { status: 403, data: { detail: { code: 'takedown_platform_origin' } } },
    });

    render(QuarantinePanel, { scope: 'organization' });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'settings.quarantine.release' })
    );

    await waitFor(() => expect(toastStore.error).toHaveBeenCalled());
    expect(post).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledTimes(2);
  });
});

describe('QuarantinePanel, platform scope is unchanged', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('reads the platform endpoint and always offers Release', async () => {
    get.mockResolvedValue({
      data: {
        files: [
          {
            uuid: 'f1',
            filename: 'a.wav',
            title: null,
            owner_uuid: 'o',
            owner_email: 'owner@example.com',
            organization_uuid: null,
            quarantine_reason: 'dmca',
            quarantined_at: null,
            quarantined_by_email: 'op@example.com',
            legal_hold: true,
            is_quarantined: true,
            takedown_origin: 'platform',
          },
        ],
        total: 1,
      },
    } as never);

    render(QuarantinePanel);

    await waitFor(() => expect(screen.getByText('owner@example.com')).toBeInTheDocument());
    expect(get.mock.calls[0][0]).toBe('/admin/files/quarantined');
    expect(screen.getByText('dmca')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'settings.quarantine.release' })).toBeEnabled();
  });
});

describe('toRows', () => {
  it('withholds the reason only for an operator takedown in the organization queue', () => {
    const [own, op] = toRows('organization', [orgRow(), operatorRow()] as never);
    expect(own.reasonWithheld).toBe(false);
    expect(own.canRelease).toBe(true);
    expect(op.reasonWithheld).toBe(true);
    expect(op.canRelease).toBe(false);
    expect(op.reason).toBeNull();
    expect(op.by).toBeNull();
  });

  it('never withholds anything from the operator itself', () => {
    const [row] = toRows('platform', [
      {
        uuid: 'f',
        filename: 'f.wav',
        title: 'Title',
        owner_uuid: 'o',
        owner_email: 'o@example.com',
        organization_uuid: 'org',
        quarantine_reason: 'why',
        quarantined_at: null,
        quarantined_by_email: 'a@example.com',
        legal_hold: false,
        is_quarantined: true,
        takedown_origin: 'organization',
      },
    ] as never);
    expect(row).toMatchObject({ reasonWithheld: false, canRelease: true, reason: 'why' });
    expect(row.label).toBe('Title');
  });
});
