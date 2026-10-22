import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => ({ default: { get: vi.fn() }, isRequestCancelled: () => false }));
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
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
import MyTakedownsPanel from './MyTakedownsPanel.svelte';

const get = vi.mocked(axiosInstance.get);

const item = (over: Record<string, unknown> = {}) => ({
  link_uuid: 'l-1',
  file_uuid: 'f-1',
  file_title: 'Board meeting',
  notice_type: 'dmca',
  received_at: '2026-10-01T00:00:00Z',
  owner_reason: 'Removed after a copyright notice',
  status: 'open',
  quarantined_at: '2026-10-02T10:00:00Z',
  outcome_at: null,
  dispute_to: 'operator',
  abuse_contact_email: 'abuse@operator.example',
  ...over,
});

describe('MyTakedownsPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the owner's file, the reason they were given and where to dispute it", async () => {
    get.mockResolvedValue({ data: { takedowns: [item()], total: 1 } } as never);
    render(MyTakedownsPanel);

    await waitFor(() => expect(screen.getByText('Board meeting')).toBeInTheDocument());
    expect(screen.getByText('Removed after a copyright notice')).toBeInTheDocument();
    expect(screen.getByText('DMCA notice')).toBeInTheDocument();
    expect(
      screen.getByText(/email a counter-notice to abuse@operator\.example and quote file ID f-1/)
    ).toBeInTheDocument();
  });

  it('routes an organization takedown to the organization, not the operator mailbox', async () => {
    get.mockResolvedValue({
      data: {
        takedowns: [item({ dispute_to: 'organization', abuse_contact_email: null })],
        total: 1,
      },
    } as never);
    render(MyTakedownsPanel);

    await waitFor(() =>
      expect(screen.getByText(/organization's administrators/)).toBeInTheDocument()
    );
    expect(screen.queryByText(/abuse@operator\.example/)).toBeNull();
  });

  it('falls back to "contact your operator" when no abuse address is configured', async () => {
    get.mockResolvedValue({
      data: { takedowns: [item({ abuse_contact_email: null })], total: 1 },
    } as never);
    render(MyTakedownsPanel);
    await waitFor(() =>
      expect(screen.getByText(/contact your service operator/)).toBeInTheDocument()
    );
  });

  it('a purged file reads as unavailable, and a released one no longer offers a dispute', async () => {
    get.mockResolvedValue({
      data: {
        takedowns: [
          item({ file_title: null, status: 'released', outcome_at: '2026-10-05T00:00:00Z' }),
        ],
        total: 1,
      },
    } as never);
    render(MyTakedownsPanel);

    await waitFor(() => expect(screen.getByText('File no longer available')).toBeInTheDocument());
    expect(screen.getByText('Released')).toBeInTheDocument();
    expect(screen.queryByText(/To dispute this/)).toBeNull();
  });

  it('asks for the caller only: no user selector is sent', async () => {
    get.mockResolvedValue({ data: { takedowns: [], total: 0 } } as never);
    render(MyTakedownsPanel);
    await waitFor(() => expect(screen.getByText('Nothing taken down')).toBeInTheDocument());
    expect(get).toHaveBeenCalledWith('/takedown-notices/mine', {
      params: { limit: 50, offset: undefined },
    });
  });
});
