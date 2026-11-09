/**
 * The opt-in anonymous usage report card (issue #1019, super_admin). It shows the server's
 * preview verbatim, every reason the server gives for forcing the report off, and a switch
 * that only ever shows what the server returned.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { TelemetryStatus } from '$lib/api/telemetry';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).englishLocale());

const api = vi.hoisted(() => ({
  getTelemetryStatus: vi.fn(),
  setTelemetryEnabled: vi.fn(),
}));
vi.mock('$lib/api/telemetry', () => api);
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import UsageReportCard from './UsageReportCard.svelte';

const PREVIEW = {
  api_key: 'phc_x',
  event: 'opentranscribe_heartbeat',
  distinct_id: '00000000-0000-0000-0000-000000000000',
  properties: { schema_version: 1, app_version: 'v0.7.0', $ip: null },
};

function status(overrides: Partial<TelemetryStatus> = {}): TelemetryStatus {
  return {
    enabled: false,
    sending: false,
    off_reasons: [],
    last_sent_at: null,
    endpoint: 'https://eu.i.posthog.com/i/v0/e/',
    preview: PREVIEW,
    ...overrides,
  };
}

const sw = () => screen.getByTestId('usage-report-switch') as HTMLInputElement;

beforeEach(() => {
  vi.clearAllMocks();
});

describe('UsageReportCard', () => {
  it('is off by default and shows the exact request the server would send', async () => {
    api.getTelemetryStatus.mockResolvedValue(status());
    render(UsageReportCard);

    const preview = await screen.findByTestId('usage-report-preview');
    expect(JSON.parse(preview.textContent ?? '')).toEqual(PREVIEW);
    expect(sw().checked).toBe(false);
    expect(screen.getByTestId('usage-report-status').textContent?.trim()).toBe('Off');
    expect(screen.getByTestId('usage-report-last-sent').textContent?.trim()).toBe('Not sent yet.');
    expect(screen.getByTestId('usage-report-endpoint').textContent).toBe(
      'https://eu.i.posthog.com/i/v0/e/'
    );
  });

  it('turns the report on and shows what the server returned', async () => {
    api.getTelemetryStatus.mockResolvedValue(status());
    api.setTelemetryEnabled.mockResolvedValue(status({ enabled: true, sending: true }));
    render(UsageReportCard);
    await screen.findByTestId('usage-report-switch');

    await fireEvent.click(sw());

    await waitFor(() => expect(sw().checked).toBe(true));
    expect(api.setTelemetryEnabled).toHaveBeenCalledWith(true);
    expect(screen.getByTestId('usage-report-status').textContent?.trim()).toBe('On');
    expect(toast.success).toHaveBeenCalledWith('Usage report turned on.');
  });

  it('names every reason the server forces it off and locks the switch', async () => {
    api.getTelemetryStatus.mockResolvedValue(
      status({ off_reasons: ['do_not_track', 'no_receiver'] })
    );
    render(UsageReportCard);

    const reasons = await screen.findByTestId('usage-report-reasons');
    expect(reasons.textContent).toContain('DO_NOT_TRACK is set on this server');
    // The shipped default: the reason must say how to allow the report, not just that it is off.
    expect(reasons.textContent).toContain('set DO_NOT_TRACK=0 in .env and restart');
    expect(reasons.textContent).toContain('No receiver is configured on this server');
    expect(sw().disabled).toBe(true);
    expect(screen.getByTestId('usage-report-status').textContent?.trim()).toBe('Forced off');
  });

  it('a stored "on" that is forced off reads as forced off and can still be switched off', async () => {
    api.getTelemetryStatus.mockResolvedValue(
      status({ enabled: true, sending: false, off_reasons: ['demo'] })
    );
    render(UsageReportCard);
    await screen.findByTestId('usage-report-reasons');

    expect(sw().disabled).toBe(false);
    expect(screen.getByTestId('usage-report-status').textContent?.trim()).toBe('Forced off');
    expect(screen.getByTestId('usage-report-reasons').textContent).toContain('demo deployment');
  });

  it('a refused change leaves the switch where the server says and explains why', async () => {
    api.getTelemetryStatus.mockResolvedValue(status());
    api.setTelemetryEnabled.mockRejectedValue({ response: { status: 409 } });
    render(UsageReportCard);
    await screen.findByTestId('usage-report-switch');

    await fireEvent.click(sw());

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error).toHaveBeenCalledWith(
      'The usage report cannot be turned on on this server.'
    );
    expect(sw().checked).toBe(false);
  });

  it('shows the last send time when there is one', async () => {
    api.getTelemetryStatus.mockResolvedValue(
      status({ enabled: true, sending: true, last_sent_at: '2026-11-07T10:00:00+00:00' })
    );
    render(UsageReportCard);

    const lastSent = await screen.findByTestId('usage-report-last-sent');
    expect(lastSent.textContent).toContain('Last sent:');
    expect(lastSent.textContent).not.toContain('{{time}}');
  });

  it('treats an answer without the expected shape as a load failure', async () => {
    api.getTelemetryStatus.mockResolvedValue({});
    render(UsageReportCard);

    expect(await screen.findByTestId('usage-report-load-failed')).toBeTruthy();
  });

  it('says so when the setting cannot be loaded', async () => {
    api.getTelemetryStatus.mockRejectedValue(new Error('down'));
    render(UsageReportCard);

    expect(await screen.findByTestId('usage-report-load-failed')).toBeTruthy();
  });
});
