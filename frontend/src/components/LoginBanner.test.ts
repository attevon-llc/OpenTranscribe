/**
 * The consent modal is the only thing standing between a user and an app where
 * every request 403s (FedRAMP AC-8). These tests pin its three behaviours that
 * matter: it reports acceptance and refusal to the parent (which owns the API
 * call and the sign-out), it distinguishes "the notice was updated" from a
 * failure, and it cannot fire two acknowledgment writes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => ({
  axiosInstance: { get: vi.fn() },
  default: { get: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { axiosInstance } from '$lib/axios';
import LoginBanner from './LoginBanner.svelte';

const BANNER = {
  enabled: true,
  text: 'You are accessing a U.S. Government Information System.',
  classification: 'CUI',
  requires_acknowledgment: true,
};

async function renderBanner(props: Record<string, unknown> = {}) {
  const onAcknowledge = vi.fn();
  const onDecline = vi.fn();
  render(LoginBanner, {
    props,
    events: { acknowledge: onAcknowledge, decline: onDecline },
  } as never);
  await screen.findByText(BANNER.text);
  return { onAcknowledge, onDecline };
}

describe('LoginBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: BANNER } as never);
  });

  it('renders the server-owned banner text from /auth/banner', async () => {
    await renderBanner();

    expect(vi.mocked(axiosInstance.get)).toHaveBeenCalledWith('/auth/banner');
    expect(screen.getByText('CUI', { selector: '.classification-header' })).toBeInTheDocument();
  });

  it('dispatches acknowledge so the parent can record consent server-side', async () => {
    const { onAcknowledge } = await renderBanner();

    await fireEvent.click(screen.getByRole('button', { name: 'loginBanner.acknowledge' }));

    expect(onAcknowledge).toHaveBeenCalledTimes(1);
  });

  it('dispatches decline instead of navigating to about:blank', async () => {
    // `window.location.href = 'about:blank'` used to live in this component and
    // is commonly blocked, leaving the user stuck on the banner. The parent now
    // signs out and returns to a clean login page.
    const { onDecline } = await renderBanner();

    await fireEvent.click(screen.getByRole('button', { name: 'loginBanner.decline' }));

    expect(onDecline).toHaveBeenCalledTimes(1);
  });

  it('says the notice changed rather than looking like a failure', async () => {
    await renderBanner({ noticeUpdated: true });

    expect(screen.getByText('loginBanner.noticeUpdated')).toBeInTheDocument();
  });

  it('hides the updated-notice line for a first-time acknowledgment', async () => {
    await renderBanner();

    expect(screen.queryByText('loginBanner.noticeUpdated')).not.toBeInTheDocument();
  });

  it('shows a failed acknowledgment in place instead of dismissing', async () => {
    await renderBanner({ errorMessage: 'loginBanner.acknowledgeFailed' });

    expect(screen.getByRole('alert')).toHaveTextContent('loginBanner.acknowledgeFailed');
  });

  it('disables both actions while the acknowledgment is being recorded', async () => {
    await renderBanner({ pending: true });

    expect(screen.getByRole('button', { name: 'loginBanner.acknowledging' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'loginBanner.decline' })).toBeDisabled();
  });

  it('renders nothing when the deployment has no banner', async () => {
    vi.mocked(axiosInstance.get).mockResolvedValue({
      data: { enabled: false, text: '', classification: '', requires_acknowledgment: false },
    } as never);

    render(LoginBanner);

    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'loginBanner.acknowledge' })
      ).not.toBeInTheDocument()
    );
  });
});
