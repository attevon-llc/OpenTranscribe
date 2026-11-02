import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t), locale: readable('en') };
});

const api = vi.hoisted(() => ({ getSelfErasureStatus: vi.fn() }));
vi.mock('$lib/api/selfErasure', () => api);

import ErasureRequestBanner from './ErasureRequestBanner.svelte';
import { lastErasureOutcome, openErasureRequest } from '$stores/selfErasure';

beforeEach(() => {
  vi.clearAllMocks();
  openErasureRequest.set(undefined);
  lastErasureOutcome.set(null);
});
afterEach(() => vi.useRealTimers());

describe('ErasureRequestBanner', () => {
  it('shows the scheduled date while a request is open', async () => {
    api.getSelfErasureStatus.mockResolvedValue({
      request: { scheduled_for: '2026-12-01T10:00:00Z' },
    });
    render(ErasureRequestBanner);
    const banner = await screen.findByTestId('erasure-banner');
    expect(banner.textContent).toContain('2026');
    expect(banner.textContent).toContain('scheduled to be erased');
  });

  it('renders nothing without a request', async () => {
    api.getSelfErasureStatus.mockResolvedValue({ request: null });
    render(ErasureRequestBanner);
    await waitFor(() => expect(api.getSelfErasureStatus).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId('erasure-banner')).toBeNull());
  });

  it('renders nothing when the status cannot be read', async () => {
    api.getSelfErasureStatus.mockRejectedValue(new Error('down'));
    render(ErasureRequestBanner);
    await waitFor(() => expect(api.getSelfErasureStatus).toHaveBeenCalled());
    expect(screen.queryByTestId('erasure-banner')).toBeNull();
  });

  it('shows the time as well as the date', async () => {
    const iso = '2026-12-01T10:30:00Z';
    api.getSelfErasureStatus.mockResolvedValue({ request: { scheduled_for: iso } });
    render(ErasureRequestBanner);
    const banner = await screen.findByTestId('erasure-banner');
    const expected = new Intl.DateTimeFormat('en', {
      dateStyle: 'long',
      timeStyle: 'short',
    }).format(new Date(iso));
    expect(banner.textContent).toContain(expected);
  });

  describe('a request the system blocked', () => {
    const blocked = (at: string) => ({
      request: null,
      last_outcome: { status: 'blocked', reason: 'last_super_admin', ledger_uuid: 'u', at },
    });

    it('shows a blocked notice for a recent block', async () => {
      api.getSelfErasureStatus.mockResolvedValue(
        blocked(new Date(Date.now() - 2 * 86_400_000).toISOString())
      );
      render(ErasureRequestBanner);
      const banner = await screen.findByTestId('erasure-banner');
      expect(banner.textContent).toContain('Your account erasure was not carried out');
    });

    it('does not show the blocked notice after seven days', async () => {
      api.getSelfErasureStatus.mockResolvedValue(
        blocked(new Date(Date.now() - 8 * 86_400_000).toISOString())
      );
      render(ErasureRequestBanner);
      await waitFor(() => expect(api.getSelfErasureStatus).toHaveBeenCalled());
      await waitFor(() => expect(get(lastErasureOutcome)).not.toBeNull());
      expect(screen.queryByTestId('erasure-banner')).toBeNull();
    });
  });

  it('re-reads the status when the tab becomes visible', async () => {
    api.getSelfErasureStatus.mockResolvedValue({ request: null });
    render(ErasureRequestBanner);
    await waitFor(() => expect(api.getSelfErasureStatus).toHaveBeenCalledTimes(1));

    api.getSelfErasureStatus.mockResolvedValue({
      request: { scheduled_for: '2026-12-01T10:00:00Z' },
    });
    document.dispatchEvent(new Event('visibilitychange'));

    await screen.findByTestId('erasure-banner');
    expect(api.getSelfErasureStatus).toHaveBeenCalledTimes(2);
  });
});
