import { tick } from 'svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).englishLocale());

const api = vi.hoisted(() => ({ getTelemetryStatus: vi.fn(), setTelemetryEnabled: vi.fn() }));
vi.mock('$lib/api/telemetry', () => api);
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

import UsageReportPrompt from './UsageReportPrompt.svelte';

const allowed = { enabled: false, off_reasons: [] };

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('UsageReportPrompt', () => {
  it('offers the opt-in when the report is allowed and off', async () => {
    api.getTelemetryStatus.mockResolvedValue(allowed);
    render(UsageReportPrompt);
    expect(await screen.findByTestId('usage-report-prompt')).toBeTruthy();
  });

  it('is a labelled non-modal region, not a dialog that never takes focus', async () => {
    api.getTelemetryStatus.mockResolvedValue(allowed);
    render(UsageReportPrompt);
    const prompt = await screen.findByTestId('usage-report-prompt');
    expect(prompt.getAttribute('role')).toBe('region');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('region', { name: /./ })).toBe(prompt);
  });

  it.each(['offline', 'pki', 'fips', 'do_not_track'])(
    'stays hidden when %s forces it off',
    async (reason) => {
      api.getTelemetryStatus.mockResolvedValue({ enabled: false, off_reasons: [reason] });
      render(UsageReportPrompt);
      await waitFor(() => expect(api.getTelemetryStatus).toHaveBeenCalled());
      // The mock is called synchronously in onMount; `visible` is set only after the await,
      // so flush the microtask and timer queues before asserting absence.
      await new Promise((resolve) => setTimeout(resolve, 0));
      await tick();
      expect(screen.queryByTestId('usage-report-prompt')).toBeNull();
    }
  );

  it('turns the report on from the prompt', async () => {
    api.getTelemetryStatus.mockResolvedValue(allowed);
    api.setTelemetryEnabled.mockResolvedValue({});
    render(UsageReportPrompt);
    await fireEvent.click(await screen.findByTestId('usage-report-prompt-enable'));
    await waitFor(() => expect(api.setTelemetryEnabled).toHaveBeenCalledWith(true));
    await waitFor(() => expect(screen.queryByTestId('usage-report-prompt')).toBeNull());
  });

  it('remembers "No thanks" and does not ask again', async () => {
    api.getTelemetryStatus.mockResolvedValue(allowed);
    const first = render(UsageReportPrompt);
    await fireEvent.click(await screen.findByTestId('usage-report-prompt-dismiss'));
    expect(api.setTelemetryEnabled).not.toHaveBeenCalled();
    first.unmount();

    api.getTelemetryStatus.mockClear();
    render(UsageReportPrompt);
    expect(api.getTelemetryStatus).not.toHaveBeenCalled();
    expect(screen.queryByTestId('usage-report-prompt')).toBeNull();
  });
});
