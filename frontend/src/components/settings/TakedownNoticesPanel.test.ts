import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), patch: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      let out = en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import TakedownNoticesPanel from './TakedownNoticesPanel.svelte';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);
const patch = vi.mocked(axiosInstance.patch);

const link = (over: Record<string, unknown> = {}) => ({
  uuid: 'l-1',
  file_uuid: 'f-1',
  file_title: 'Board meeting',
  file_exists: true,
  owner_user_uuid: 'u-1',
  organization_uuid: null,
  owner_reason: 'Removed after a copyright notice',
  status: 'open',
  quarantined_at: '2026-10-02T10:00:00Z',
  counter_notice_received_at: null,
  outcome_at: null,
  outcome_note: null,
  legal_hold_placed: true,
  ...over,
});

const notice = (over: Record<string, unknown> = {}) => ({
  uuid: 'n-1',
  notice_type: 'dmca',
  received_at: '2026-10-01T00:00:00Z',
  requester_reference: 'CASE-42',
  basis: 'internal legal basis text',
  recorded_by_authority: 'platform',
  handled_by_user_uuid: null,
  created_at: '2026-10-01T00:00:00Z',
  status: 'open',
  links: [link()],
  ...over,
});

describe('TakedownNoticesPanel', () => {
  // The list and the per-notice read share one mock, answered by path.
  function serve(listed: unknown[], detail?: (uuid: string) => unknown) {
    get.mockImplementation(async (path: string) => {
      const match = /^\/admin\/takedown-notices\/([^/]+)$/.exec(path);
      if (match) return { data: detail ? detail(match[1]) : listed[0] } as never;
      return { data: { notices: listed, total: listed.length } } as never;
    });
  }

  beforeEach(() => {
    vi.clearAllMocks();
    serve([notice()]);
  });

  it('re-reads a notice when it is opened, so a stale list cannot show a released file as open', async () => {
    serve([notice()], () =>
      notice({ links: [link({ status: 'released', outcome_at: '2026-10-05T00:00:00Z' })] })
    );
    render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());

    await fireEvent.click(screen.getByRole('button', { name: /\d/ }));

    await waitFor(() => expect(screen.getByText('Released')).toBeInTheDocument());
    expect(get).toHaveBeenCalledWith('/admin/takedown-notices/n-1');
    expect(screen.queryByRole('button', { name: 'Mark upheld' })).toBeNull();
  });

  it('lists notices with their derived status, and expands one to show its basis and files', async () => {
    render(TakedownNoticesPanel);

    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());
    expect(get.mock.calls[0][0]).toBe('/admin/takedown-notices');
    expect(screen.queryByText('internal legal basis text')).toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: /\d/ }));

    expect(screen.getByText(/internal legal basis text/)).toBeInTheDocument();
    expect(screen.getByText('Board meeting')).toBeInTheDocument();
    expect(screen.getByText('Removed after a copyright notice')).toBeInTheDocument();
  });

  it('records a counter-notice for a file under takedown and shows the updated status', async () => {
    patch.mockResolvedValue({
      data: notice({ links: [link({ status: 'counter_notice' })] }),
    } as never);
    render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());
    await fireEvent.click(screen.getByRole('button', { name: /\d/ }));

    await fireEvent.click(screen.getByRole('button', { name: 'Record counter-notice' }));

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith('/admin/takedown-notices/n-1/files/f-1', {
        status: 'counter_notice',
      })
    );
    await waitFor(() => expect(screen.getByText('Counter-notice received')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Record counter-notice' })).toBeNull();
  });

  it('offers no outcome action for a released file or a purged one', async () => {
    serve([
      notice({
        links: [
          link({ status: 'released', outcome_at: '2026-10-05T00:00:00Z' }),
          link({ uuid: 'l-2', file_uuid: 'f-2', file_exists: false, file_title: null }),
        ],
      }),
    ]);
    render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());
    await fireEvent.click(screen.getByRole('button', { name: /\d/ }));

    expect(screen.queryByRole('button', { name: 'Record counter-notice' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Mark upheld' })).toBeNull();
    expect(screen.getByText('File deleted')).toBeInTheDocument();
  });

  it('notify names no recipient: only the event goes to the server', async () => {
    post.mockResolvedValue({
      data: { sent: 1, failed: 0, skipped_no_transport: 0, already_notified: 0 },
    } as never);
    render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());
    await fireEvent.click(screen.getByRole('button', { name: /\d/ }));

    await fireEvent.click(screen.getByRole('button', { name: 'Email the owners' }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/admin/takedown-notices/n-1/notify', {
        event: 'quarantined',
      })
    );
    expect(toastStore.success).toHaveBeenCalledWith('1 owner(s) emailed');
  });

  it('says so when no mail transport exists instead of reporting a send', async () => {
    post.mockResolvedValue({
      data: { sent: 0, failed: 0, skipped_no_transport: 1, already_notified: 0 },
    } as never);
    render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());
    await fireEvent.click(screen.getByRole('button', { name: /\d/ }));
    await fireEvent.click(screen.getByRole('button', { name: 'Email the owners' }));

    await waitFor(() => expect(toastStore.warning).toHaveBeenCalled());
    expect(toastStore.success).not.toHaveBeenCalled();
  });

  it('a fully released notice offers the restore email, never a takedown email', async () => {
    serve([
      notice({
        status: 'resolved',
        links: [link({ status: 'released', outcome_at: '2026-10-05T00:00:00Z' })],
      }),
    ]);
    post.mockResolvedValue({
      data: { sent: 1, failed: 0, skipped_no_transport: 0, already_notified: 0 },
    } as never);
    render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());
    await fireEvent.click(screen.getByRole('button', { name: /\d/ }));

    expect(screen.queryByRole('button', { name: 'Email the owners' })).toBeNull();
    await fireEvent.click(
      screen.getByRole('button', { name: 'Email the owners that access was restored' })
    );

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/admin/takedown-notices/n-1/notify', {
        event: 'released',
      })
    );
  });

  it('records a new notice from the form, which is invalid until reference and basis exist', async () => {
    post.mockResolvedValue({ data: notice({ links: [] }) } as never);
    const { container } = render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('CASE-42')).toBeInTheDocument());

    await fireEvent.click(screen.getByRole('button', { name: 'Record a notice' }));
    const save = screen.getByRole('button', { name: 'Save notice' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await fireEvent.input(container.querySelector('#panel-notice-reference')!, {
      target: { value: 'CASE-9' },
    });
    await fireEvent.input(container.querySelector('#panel-notice-basis')!, {
      target: { value: 'basis' },
    });
    expect(save.disabled).toBe(false);
    await fireEvent.click(save);

    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][0]).toBe('/admin/takedown-notices');
    expect(post.mock.calls[0][1]).toMatchObject({
      notice_type: 'dmca',
      requester_reference: 'CASE-9',
      basis: 'basis',
    });
  });

  it('shows a clear empty state', async () => {
    serve([]);
    render(TakedownNoticesPanel);
    await waitFor(() => expect(screen.getByText('No notices recorded')).toBeInTheDocument());
  });
});
