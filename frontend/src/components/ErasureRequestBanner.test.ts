import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

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
import { openErasureRequest } from '$stores/selfErasure';

beforeEach(() => {
  vi.clearAllMocks();
  openErasureRequest.set(undefined);
});

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
});
