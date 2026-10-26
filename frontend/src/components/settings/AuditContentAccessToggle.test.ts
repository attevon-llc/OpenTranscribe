/**
 * The super_admin switch for content-access auditing (#1277). The server decides what is in
 * effect; the component renders it, sends the change, and re-reads on failure so the switch
 * never shows a value the server does not hold.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t) };
});

const api = vi.hoisted(() => ({ getContentAccessAudit: vi.fn(), setContentAccessAudit: vi.fn() }));
vi.mock('$lib/api/admin', () => ({ AdminApi: api }));

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import AuditContentAccessToggle from './AuditContentAccessToggle.svelte';

const off = {
  enabled: false,
  capability_available: true,
  active: false,
  dedup_seconds: 900,
  event_bytes_estimate: 800,
  bytes_per_active_user_per_day_estimate: 34000,
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('AuditContentAccessToggle', () => {
  it('shows the switch off with the storage estimate, from the server numbers', async () => {
    api.getContentAccessAudit.mockResolvedValue(off);
    render(AuditContentAccessToggle);

    const toggle = (await screen.findByRole('checkbox')) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    expect(
      screen.getByText(/Roughly 34 KB per active user per day, about 1\.0 GB a month/)
    ).toBeInTheDocument();
    expect(screen.getByText(/within 15 minutes is recorded once/)).toBeInTheDocument();
    expect(screen.queryByTestId('content-access-unavailable')).toBeNull();
  });

  it('sends the change, reflects the server answer and confirms', async () => {
    api.getContentAccessAudit.mockResolvedValue(off);
    api.setContentAccessAudit.mockResolvedValue({ ...off, enabled: true, active: true });
    render(AuditContentAccessToggle);

    const toggle = (await screen.findByRole('checkbox')) as HTMLInputElement;
    await fireEvent.click(toggle);

    await waitFor(() => expect(api.setContentAccessAudit).toHaveBeenCalledWith(true));
    await waitFor(() => expect(toggle.checked).toBe(true));
    expect(toast.success).toHaveBeenCalledWith('Content access auditing is on');
  });

  it('re-reads the server value when the change is refused', async () => {
    api.getContentAccessAudit.mockResolvedValue(off);
    api.setContentAccessAudit.mockRejectedValue(new Error('403'));
    render(AuditContentAccessToggle);

    const toggle = (await screen.findByRole('checkbox')) as HTMLInputElement;
    await fireEvent.click(toggle);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Could not change content access auditing')
    );
    await waitFor(() => expect(toggle.checked).toBe(false));
    expect(api.getContentAccessAudit).toHaveBeenCalledTimes(2);
  });

  it('says so when the edition withholds the feature', async () => {
    api.getContentAccessAudit.mockResolvedValue({
      ...off,
      enabled: true,
      capability_available: false,
    });
    render(AuditContentAccessToggle);

    expect(await screen.findByTestId('content-access-unavailable')).toBeInTheDocument();
  });
});
