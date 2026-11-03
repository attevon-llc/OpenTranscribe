/**
 * "Erase my account" (issue #1354): hidden while the deployment has it off, a typed confirmation
 * plus re-entry before the request can be sent, and a cancel that exists only until the scheduled
 * time.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
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

const api = vi.hoisted(() => ({
  getSelfErasureStatus: vi.fn(),
  requestSelfErasure: vi.fn(),
  cancelSelfErasure: vi.fn(),
}));
vi.mock('$lib/api/selfErasure', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/selfErasure')>();
  return { ...actual, ...api };
});
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import AccountErasureCard from './AccountErasureCard.svelte';
import { openErasureRequest } from '$stores/selfErasure';

const HOUR = 3_600_000;
const receipt = (scheduledInMs: number) => ({
  ledger_uuid: '0192a-uuid',
  requested_at: new Date(Date.now() - HOUR).toISOString(),
  scheduled_for: new Date(Date.now() + scheduledInMs).toISOString(),
  sla_due_at: new Date(Date.now() + 30 * 24 * HOUR).toISOString(),
  request_digest: 'a'.repeat(64),
  status: 'scheduled' as const,
  cancelled_at: null,
});
const status = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  grace_days: 14,
  can_reauthenticate: true,
  request: null,
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  openErasureRequest.set(undefined);
});
afterEach(() => vi.useRealTimers());

describe('AccountErasureCard', () => {
  it('renders nothing when the deployment has self-service erasure off', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status({ enabled: false }));
    render(AccountErasureCard);
    await waitFor(() => expect(api.getSelfErasureStatus).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId('account-erasure-card-loading')).toBeNull());
    expect(screen.queryByTestId('account-erasure-card')).toBeNull();
  });

  it('keeps the request button disabled until the word is typed and a password is given', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status());
    render(AccountErasureCard);
    const submit = (await screen.findByTestId('erasure-submit')) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    await fireEvent.input(screen.getByTestId('erasure-confirm'), { target: { value: 'ERAS' } });
    await fireEvent.input(screen.getByTestId('erasure-password'), { target: { value: 'pw' } });
    expect(submit.disabled).toBe(true);

    await fireEvent.input(screen.getByTestId('erasure-confirm'), { target: { value: 'ERASE' } });
    expect(submit.disabled).toBe(false);
  });

  it('sends the confirmation and password, then shows the receipt and updates the shared store', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status());
    api.requestSelfErasure.mockResolvedValue(receipt(14 * 24 * HOUR));
    render(AccountErasureCard);
    await fireEvent.input(await screen.findByTestId('erasure-confirm'), {
      target: { value: 'ERASE' },
    });
    await fireEvent.input(screen.getByTestId('erasure-password'), { target: { value: 'secret' } });
    await fireEvent.click(screen.getByTestId('erasure-submit'));

    await screen.findByTestId('erasure-scheduled');
    expect(api.requestSelfErasure).toHaveBeenCalledWith({ confirm: 'ERASE', password: 'secret' });
    expect(screen.getByText('a'.repeat(64))).toBeTruthy();
    expect(get(openErasureRequest)?.ledger_uuid).toBe('0192a-uuid');
  });

  it('shows a fixed sentence for a refused request and never the server text', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status());
    api.requestSelfErasure.mockRejectedValue({
      response: { data: { detail: { code: 'reauth_required', message: 'raw server text' } } },
    });
    render(AccountErasureCard);
    await fireEvent.input(await screen.findByTestId('erasure-confirm'), {
      target: { value: 'ERASE' },
    });
    await fireEvent.input(screen.getByTestId('erasure-password'), { target: { value: 'bad' } });
    await fireEvent.click(screen.getByTestId('erasure-submit'));

    const error = await screen.findByTestId('erasure-error');
    expect(error.textContent).toBe('The password or code was not accepted.');
    expect(screen.queryByText('raw server text')).toBeNull();
  });

  it('offers cancel before the scheduled time and cancelling clears the request', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status({ request: receipt(5 * 24 * HOUR) }));
    api.cancelSelfErasure.mockResolvedValue({ ...receipt(0), status: 'cancelled' });
    render(AccountErasureCard);
    await fireEvent.click(await screen.findByTestId('erasure-cancel'));

    await waitFor(() => expect(api.cancelSelfErasure).toHaveBeenCalledWith('0192a-uuid'));
    await waitFor(() => expect(screen.queryByTestId('erasure-scheduled')).toBeNull());
    expect(get(openErasureRequest)).toBeNull();
  });

  it('has no cancel button once the scheduled time has passed', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status({ request: receipt(-HOUR) }));
    render(AccountErasureCard);
    await screen.findByTestId('erasure-scheduled');
    expect(screen.queryByTestId('erasure-cancel')).toBeNull();
  });

  it('explains instead of offering a form when the account cannot confirm', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status({ can_reauthenticate: false }));
    render(AccountErasureCard);
    await screen.findByText(/cannot confirm the request here/);
    expect(screen.queryByTestId('erasure-submit')).toBeNull();
  });

  it('tells the person their request was blocked as the last super administrator', async () => {
    api.getSelfErasureStatus.mockResolvedValue(
      status({
        last_outcome: {
          status: 'blocked',
          reason: 'last_super_admin',
          ledger_uuid: 'u',
          at: new Date().toISOString(),
        },
      })
    );
    render(AccountErasureCard);

    const notice = await screen.findByTestId('erasure-blocked');
    expect(notice.textContent).toContain('last active super administrator');
    // The form stays: they may ask again once another super administrator exists.
    expect(screen.getByTestId('erasure-submit')).toBeTruthy();
  });

  it('says the request can no longer be cancelled on 409 not_cancellable', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status({ request: receipt(HOUR) }));
    api.cancelSelfErasure.mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'not_cancellable' } } },
    });
    render(AccountErasureCard);

    await fireEvent.click(await screen.findByTestId('erasure-cancel'));

    expect((await screen.findByTestId('erasure-error')).textContent).toContain(
      'can no longer be cancelled'
    );
  });

  it('says the request no longer exists on 404 not_found and reloads', async () => {
    api.getSelfErasureStatus.mockResolvedValue(status({ request: receipt(HOUR) }));
    api.cancelSelfErasure.mockRejectedValue({
      response: { status: 404, data: { detail: { code: 'not_found' } } },
    });
    render(AccountErasureCard);

    await fireEvent.click(await screen.findByTestId('erasure-cancel'));

    expect((await screen.findByTestId('erasure-error')).textContent).toContain('no longer exists');
    await waitFor(() => expect(api.getSelfErasureStatus).toHaveBeenCalledTimes(2));
  });
});
